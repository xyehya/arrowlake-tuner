"""G-114: SPEC-v3 section 9 (CPU facts) through the helper: sim tables (F1/F2), tune.read ``fused`` / ``turbo`` (F5/F7), tune.plan ``effective_v`` /
``clamped_by`` / ``vccia`` / warnings (F4/F6/F8) and ``REFUSED TURBO_ORDER`` (F5). Simulator and the real advanced policy file only; no hardware.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from arltuner.controls import XTU_SHAPE_CAVEAT
from arltuner.curves import CurveStore
from arltuner.helper.ops_measure import STOCK_VID, MeasureWiring, RunEnv, SimCpufreq, SimMeasureIO, sim_memory
from arltuner.mailbox import VfWord
from arltuner.msr import SimConfig, SimMsrPort

from ._support import Harness
from .test_tune import ADVANCED, TuneCase, error_code, row, rows, texts

P_TABLE = [8, 20, 30, 48, 53, 55, 56, 57]
E_TABLE = [8, 15, 23, 30, 40, 46]
FUSED = {  # examples/reference-285k/notes/platform.md section 4 [MEASURED win 2026-10-01 / lnx 2026-10-02]: cpu -> (word, V)
    0: (0x5B439, 1.4258), 1: (0x53839, 1.3047), 2: (0x57439, 1.3633), 3: (0x59439, 1.3945), 4: (0x54439, 1.3164), 5: (0x56C39, 1.3555), 6: (0x56039, 1.3438),
    7: (0x58039, 1.3750), 8: (0x4A02E, 1.1563), 12: (0x48C2E, 1.1367), 16: (0x48C2E, 1.1367), 20: (0x48C2E, 1.1367),
}
STOCK = SimConfig.measured(per_point=True, reject_zero_fields=True, point_offsets={})  # runtime offsets 0, as the baseline curve was measured


def baseline_record(created: str = "20261011T181718Z-y4c7") -> dict[str, Any]:
    """A stock baseline as vf.measure stores it: ok points from the measured table, 56/57x of the non-favored P-cores and E 23x absent (unreachable)."""
    units = {}
    for unit, table in STOCK_VID.items():
        cpus = [int(unit[1:])] if unit.startswith("P") else list(range(8 + 4 * int(unit[2:]), 12 + 4 * int(unit[2:])))
        points = [{"ratio": r, "mhz": r * 100, "vid_v": v, "vid_min": v, "vid_max": v, "kept": 10, "total": 10, "status": "ok"} for r, v in table.items()]
        units[unit] = {"cpus": cpus, "points": points}
    return {"id": created, "name": "Stock", "created": "2026-10-11T18:17:18Z", "boot_id": None, "mode": "quick", "complete": True, "kernel": "sim",
            "tuning_state": {"kind": "unknown"}, "temps": {"min_c": 28, "max_c": 80}, "units": units, "baseline": True}


class FactsCase(TuneCase):
    def boot_with(self, config: SimConfig | None = None, baseline: bool = True, **overrides: Any) -> Harness:
        h = self.boot(config or STOCK, **overrides)
        if baseline:
            store = CurveStore(self.tmp / "curves")
            store.save(baseline_record())
            cf = SimCpufreq()
            io = SimMeasureIO(cf, cf.model)
            h.helper.measure_wiring = MeasureWiring(cpufreq=cf, make_run=lambda: RunEnv(io, io.read_perf), store=store, journal_path=self.tmp / "j.json", mem_fn=sim_memory)
        return h


class SimTableTests(FactsCase):
    def test_every_p_core_has_the_same_eight_points_and_every_e_cluster_the_same_six(self) -> None:
        """F1 [MEASURED lnx 2026-10-02]: the tables are fixed per domain; the simulator no longer invents a per-core table."""
        self.boot(STOCK)
        for cpu in range(8):
            self.assertEqual([self.sim.vf(cpu, 0, p).ratio for p in range(1, 9)], P_TABLE, cpu)
        for cpu in (8, 12, 16, 20):
            self.assertEqual([self.sim.vf(cpu, 0, p).ratio for p in range(1, 7)], E_TABLE, cpu)

    def test_oc_word_fused_words_and_favored_words_are_the_measured_ones(self) -> None:
        """F2: P0's OC word is 0x39 (BIOS favored 57x), the others 0; 0x09 and 0x1C as in examples/reference-285k/notes/platform.md section 4."""
        self.boot(STOCK)
        self.assertEqual(self.sim.vf(0, 0, 0).to_u32(), 0x39)
        self.assertEqual({self.sim.vf(c, 0, 0).to_u32() for c in range(1, 24)}, {0})
        for cpu, (word, _) in FUSED.items():
            self.assertEqual(self.sim.fused_word(cpu), word, cpu)
        self.assertEqual([self.sim.favored_word(c) for c in range(8)], [0x3939, 0x3737, 0x3737, 0x3939, 0x3737, 0x3737, 0x3737, 0x3737])
        self.assertEqual(self.sim.favored_word(8), 0x2E30)

    def test_measured_starts_every_point_at_offset_zero_and_the_old_offsets_are_opt_in(self) -> None:
        """G-117: cold-boot stock has runtime point offsets 0; ``POINT_OFFSETS_ARL_S`` (the old BIOS-like -51/-15/-51) is only an opt-in."""
        from arltuner.msr.sim import POINT_OFFSETS_ARL_S

        self.assertEqual(SimConfig.measured().point_offsets, {})
        self.boot(SimConfig.measured(per_point=True))
        self.assertEqual({self.sim.vf(c, 0, p).offset_raw for c in (0, 3, 8) for p in range(1, 7)}, {0})
        self.boot(SimConfig.measured(per_point=True, point_offsets=dict(POINT_OFFSETS_ARL_S)))
        self.assertEqual((self.sim.vf(0, 0, 5).offset_raw, self.sim.vf(0, 0, 6).offset_raw, self.sim.vf(0, 0, 8).offset_raw), (-51, -15, -51))

    def test_the_turbo_registers_are_the_measured_ones(self) -> None:
        self.boot(STOCK)
        self.assertEqual(self.sim.rdmsr(0, 0x1AD), 0x3737373737373939)
        self.assertEqual(self.sim.rdmsr(8, 0x651), 0x100C080604030201)  # the E bucket counts are not part of the edit
        self.assertEqual(self.sim.rdmsr(0, 0x1AE), 0x0807060504030201)
        self.assertEqual(self.sim.rdmsr(0, 0x650), 0x3030303030303030)
        self.assertEqual(self.sim.rdmsr(0, 0x651), 0x100C080604030201)


class ReadFactsTests(FactsCase):
    def test_tune_read_returns_fused_and_turbo_and_labels_the_points(self) -> None:
        h = self.boot_with(baseline=False)
        res = self.read(h)
        self.assertEqual(sorted(res["fused"], key=int), [str(c) for c in sorted(FUSED)])
        for cpu, (word, volts) in FUSED.items():
            f = res["fused"][str(cpu)]
            self.assertEqual((f["word"], f["ratio"], f["v"]), (f"0x{word:08x}", word & 0xFF, volts), cpu)
        self.assertEqual(res["fused"]["0"]["ratio"], 57)
        self.assertEqual(res["fused"]["8"]["ratio"], 46)
        t = res["turbo"]
        self.assertEqual(t["P"], {"ratios": [57, 57, 55, 55, 55, 55, 55, 55], "counts": [1, 2, 3, 4, 5, 6, 7, 8]})
        self.assertEqual(t["E"], {"ratios": [48] * 8, "counts": [1, 2, 3, 4, 6, 8, 12, 16]})
        self.assertEqual(t["favored"], {str(c): (57 if c in (0, 3) else 55) for c in range(8)})
        self.assertEqual(res["tables"], {"e": [48] * 8, "p": [57, 57, 55, 55, 55, 55, 55, 55]})  # G-112 shape kept
        labels = [p["point_label"] for p in res["tune"]["P0"]["cpus"][0]["points"]]
        self.assertEqual(labels, ["OC (point 9)"] + [f"point {n}" for n in range(1, 9)])
        self.assertEqual(res["tune"]["EC0"]["cpus"][0]["points"][0]["point_label"], "OC (point 7)")
        self.assertEqual(res["tune"]["EC0"]["cpus"][0]["points"][6]["point_label"], "point 6")

    def test_the_extra_reads_are_28_mailbox_reads_and_4_rdmsr_and_never_a_write(self) -> None:
        h = self.boot_with(baseline=False)
        self.read(h, cpus=[0])  # the dump of CPU 0 (10 reads: OC word, 8 points, the rc 0x11) plus the extras of the same call
        cmds = [r.cmd for r in self.sim.requests]
        self.assertEqual((cmds.count(0x09), cmds.count(0x1C), cmds.count(0x10)), (12, 12, 10))  # 0x1C: 8 P-cores + 4 E clusters (G-137)
        self.assertEqual((cmds.count(0x01), cmds.count(0x28)), (2, 2))  # G-137 (D-039): the CPU's own limits, one P CPU and one E CPU each
        self.assertEqual([(r.cmd, r.cpu) for r in self.sim.requests if r.cmd in (0x01, 0x28)], [(0x01, 0), (0x28, 0), (0x01, 8), (0x28, 8)])
        self.assertEqual(len(self.sim.writes), len(self.sim.requests))  # every mailbox read is exactly one WRMSR 0x150, nothing else
        self.assertTrue(all(msr == 0x150 for _, msr, _ in self.sim.writes))
        self.assertTrue(all(r.cmd in (0x09, 0x1C, 0x10, 0x01, 0x28) for r in self.sim.requests))

    def test_an_old_policy_without_bucket_count_reads_keeps_the_ratio_tables(self) -> None:
        text = ADVANCED.read_text()
        for name in ("msr_0x1ae_read", "msr_0x651_read"):  # a policy seeded before 2026-10-11 has no allow entry for the bucket counts
            start = text.index(f'name = "{name}"')
            start = text.rindex("[[msr_allow]]", 0, start)
            text = text[:start] + text[text.index("[[msr_allow]]", start + 1):]
        self.policy.write_text(text)
        h = self.boot_with(baseline=False)
        res = self.read(h)
        self.assertEqual(res["tables"]["p"], [57, 57, 55, 55, 55, 55, 55, 55])
        self.assertIsNone(res["turbo"]["P"]["counts"])
        self.assertIn("bucket counts not read", res["extras_notes"]["counts"])

    def test_an_oc_ratio_above_the_top_point_reads_as_the_oc_point_not_an_extra_fused_point(self) -> None:
        cfg = SimConfig.measured(per_point=True, reject_zero_fields=True, point_offsets={}, oc_adds_point=True, initial_vf={0: VfWord(58, 0, False, 0)})
        h = self.boot_with(cfg, baseline=False)
        res = self.read(h, cpus=[0])
        pts = res["tune"]["P0"]["cpus"][0]["points"]
        self.assertEqual(len(pts), 10)  # OC word + 8 fused + the OC point
        self.assertEqual((pts[9]["point"], pts[9]["point_label"], pts[9].get("oc_point")), (9, "OC (point 9)", True))
        self.assertNotIn("oc_point", pts[8])
        self.assertIn("rc 0x11", res["units"][0]["cpus"][0]["end"])  # the table end is not an error
        plan = h.call("pp", "tune.plan", {"intent": rows(row("P0", points={"58": -10}))})
        self.assertEqual(error_code(plan), "BAD_REQUEST")  # no per-point write at the OC point


class PlanFactsTests(FactsCase):
    def test_the_measured_p0_57x_case_is_clamped_up_to_the_56x_voltage(self) -> None:
        h = self.boot_with()
        self.read(h)
        p = self.plan(h, rows(row("P0", points={"57": -50})))
        a = p["after"]["P0"]["8"]
        self.assertEqual((a["ratio"], a["clamped_by"]), (57, 7))
        self.assertAlmostEqual(a["effective_v"], 1.386, places=4)  # not 1.4288 - 0.05 = 1.379
        self.assertEqual(p["before"]["P0"]["8"]["clamped_by"], None)
        self.assertAlmostEqual(p["before"]["P0"]["8"]["effective_v"], 1.4288, places=4)
        self.assertTrue(any("P0 point 8 (57x)" in w and "clamped up" in w and "point 7" in w for w in texts(p)), p["warnings"])
        self.assertIn("CLAMPED", [w["code"] for w in p["warnings"]])
        view = p["predicted"]["P0"]
        self.assertEqual(sorted(view, key=int), [str(n) for n in range(1, 9)])  # every point of a touched unit
        self.assertEqual((view["8"]["clamped_by"], view["7"]["clamped_by"]), (7, None))
        self.assertAlmostEqual(view["5"]["effective_v"], 1.216, places=4)
        self.assertNotIn("P1", p["predicted"])

    def test_a_point_that_is_not_written_but_moves_with_the_clamp_is_warned_about(self) -> None:
        h = self.boot_with()
        self.read(h)
        p = self.plan(h, rows(row("P2", points={"53": 150})))  # 53x 1.271 -> 1.421 V: the 55x (1.366) and 56x/57x (fused 1.3633) points ride up
        self.assertEqual(p["after"]["P2"]["5"]["clamped_by"], None)
        view = p["predicted"]["P2"]
        self.assertEqual((view["6"]["clamped_by"], view["7"]["clamped_by"], view["8"]["clamped_by"]), (5, 5, 5))
        self.assertTrue(any("P2 point 6 (55x)" in w and "point 5" in w for w in texts(p)), p["warnings"])

    def test_an_oc_target_below_the_top_fused_point_is_clamped_and_predicted_at_that_point(self) -> None:
        """G-120 fixes 2 + 3 [MEASURED lnx 2026-10-11]: EC1 48x with 1.117 V < point 6 (1.137 V) read back 48x = 47x = point 6; 1.147 V gave 1.1475 V."""
        h = self.boot_with()
        self.read(h)
        p = self.plan(h, rows(row("EC1", ratio=48, v=1.117)))
        oc = p["after"]["EC1"]["0"]
        self.assertEqual((oc["ratio"], oc["clamped_by"]), (48, 6))
        self.assertAlmostEqual(oc["effective_v"], 1.137, places=4)  # max(target, V(point 6)), not the 1.117 asked
        self.assertTrue(any("EC1 OC point (48x)" in w and "held up" in w for w in texts(p)), p["warnings"])
        self.assertIn("CLAMPED", [w["code"] for w in p["warnings"]])
        p = self.plan(h, rows(row("EC1", ratio=48, v=1.147)))  # above point 6: nothing clamped, the point sits at its target
        oc = p["after"]["EC1"]["0"]
        self.assertEqual((oc["clamped_by"], round(oc["effective_v"], 3)), (None, 1.147))
        self.assertFalse(any("OC point" in w and w != XTU_SHAPE_CAVEAT for w in texts(p)), p["warnings"])  # G-122: the standing caveat names the OC point

    def test_a_whole_curve_offset_warns_about_no_point_it_does_not_change(self) -> None:
        """G-120 fix 2: the plan -10 mV on every P-core clamps nothing it does not touch (no "57x offset +0.0 clamped" on cores that cannot reach 56/57x)."""
        h = self.boot_with()
        self.read(h)
        p = self.plan(h, rows(row("P0", curve=-10), row("P1", curve=-10), row("P2", curve=-10), row("P4", curve=-10)))
        self.assertEqual([w for w in texts(p) if "clamped" in w], [], p["warnings"])
        self.assertNotIn("CLAMPED", [w["code"] for w in p["warnings"]])

    def test_a_whole_curve_offset_on_a_core_without_an_oc_point_predicts_the_word_the_hardware_keeps(self) -> None:
        h = self.boot_with()
        self.read(h)
        p = self.plan(h, rows(row("P0", curve=-10)))
        oc = p["after"]["P0"]["0"]
        self.assertEqual((oc["ratio"], oc["v_target_v"], round(oc["offset_mv"], 1)), (57, None, -9.8))  # 57x = the top fused ratio: ratio | offset << 21, no target

    def test_effective_v_adds_the_offset_to_the_measured_stock_vid(self) -> None:
        h = self.boot_with()
        self.read(h)
        p = self.plan(h, rows(row("P4", points={"53": -50, "55": -30})))
        self.assertAlmostEqual(p["after"]["P4"]["5"]["effective_v"], 1.226 + p["after"]["P4"]["5"]["offset_mv"] / 1000, places=3)  # the offset is rounded to 1/1024 V
        self.assertAlmostEqual(p["after"]["P4"]["6"]["effective_v"], 1.319 + p["after"]["P4"]["6"]["offset_mv"] / 1000, places=3)
        self.assertAlmostEqual(p["after"]["P4"]["5"]["effective_v"], 1.176, delta=0.0005)
        self.assertEqual(p["after"]["P4"]["5"]["clamped_by"], None)

    def test_no_baseline_means_no_predictions_and_one_warning(self) -> None:
        h = self.boot_with(baseline=False)
        self.read(h)
        p = self.plan(h, rows(row("P0", points={"57": -50})))
        self.assertEqual((p["after"]["P0"]["8"]["effective_v"], p["after"]["P0"]["8"]["clamped_by"]), (None, None))
        self.assertIsNone(p["vccia"])
        self.assertIsNone(p["predicted"])
        self.assertEqual([w["code"] for w in p["warnings"] if w["code"] == "NO_BASELINE"], ["NO_BASELINE"])
        self.assertTrue(texts(p)[[w["code"] for w in p["warnings"]].index("NO_BASELINE")].startswith("measure a stock baseline for predictions"))

    def test_vccia_is_set_by_the_unit_with_the_highest_allowed_vid(self) -> None:
        """F6 (D-036): stock, all-core: the highest requested VID is P7 at 55x (1.376 V); no ring floor and no margin are assumed. Lowering a unit that does not set the maximum does not lower it; lowering P7 hands it to P2."""
        h = self.boot_with()
        self.read(h)
        p = self.plan(h, rows(row("P0", points={"55": -40})))
        v = p["vccia"]
        self.assertEqual(v["set_by"], {"unit": "P7", "ratio": 55})
        self.assertAlmostEqual(v["predicted_v"], 1.376, places=3)
        self.assertEqual((v["label"], v["margin_v"]), ("highest requested VID among units", None))
        self.assertAlmostEqual(v["before_v"], 1.376, places=3)  # P0 does not set VccIA: nothing changes
        self.assertEqual(v["light_load"]["set_by"], {"unit": "P0", "ratio": 57})  # 1-2 active cores: the favored 57x points count
        p2 = self.plan(h, rows(row("P7", points={"55": -100})))
        self.assertEqual(p2["vccia"]["set_by"], {"unit": "P2", "ratio": 55})
        self.assertAlmostEqual(p2["vccia"]["predicted_v"], 1.366, places=3)
        self.assertAlmostEqual(p2["vccia"]["before_v"], 1.376, places=3)
        self.assertTrue(any("VccIA is a prediction" in w for w in texts(p)))
        c = p2["vccia"]["contributors"]
        self.assertEqual(set(c), {"P", "E", "ring"})
        self.assertEqual((c["P"]["unit"], c["P"]["ratio"], c["E"]["unit"], c["E"]["ratio"], c["ring"]), ("P2", 55, "EC0", 48, None))  # the ring ratio is not read: no figure

    def test_e_40x_offset_and_e_target_above_1_2_v_are_warned_about(self) -> None:
        h = self.boot_with()
        self.read(h)
        p = self.plan(h, rows(row("EC1", points={"40": -20}), row("EC2", v=1.25)))
        self.assertTrue(any("EC1" in w and "40x" in w and "Community report" in w for w in texts(p)), p["warnings"])
        self.assertTrue(any("EC2" in w and "1.250 V" in w and "WHEA" in w and "Community report" in w for w in texts(p)), p["warnings"])
        self.assertFalse(any("this machine" in w or "MEASURED" in w for w in texts(p)), p["warnings"])  # D-036: no reference-machine lore in user-facing text
        quiet = self.plan(h, rows(row("EC1", points={"46": -25})))
        self.assertFalse(any("40x" in w or "WHEA" in w for w in texts(quiet)), quiet["warnings"])

    def test_positive_offsets_still_warn(self) -> None:
        h = self.boot_with()
        self.read(h)
        p = self.plan(h, rows(row("P6", points={"53": 15})))
        self.assertTrue(any("positive offset" in w and "P6" in w for w in texts(p)), p["warnings"])


class TurboOrderTests(FactsCase):
    def accepted(self, p_table: list[int]) -> Path:
        acc = self.tmp / "accepted"
        (acc / "profiles").mkdir(parents=True)
        (acc / "CURRENT").write_text("policy = policies/x.toml\nprofile = profiles/t.toml\n")
        (acc / "profiles/t.toml").write_text(f'[meta]\nname = "t"\nplatform = "arl-s-285k"\n[[point]]\nkind = "P"\nindex = 0\npoint = 5\noffset_mv = -50\nexpect_ratio = 53\n[turbo]\np = {p_table}\n')
        return acc

    def test_a_table_that_rises_with_the_active_core_count_is_refused(self) -> None:
        h = self.boot_with(baseline=False, accepted_dir=self.accepted([55, 57, 55, 55, 55, 55, 55, 55]))
        r = h.call("t", "tune.plan", {"intent": {"restore": "accepted"}})
        self.assertEqual(error_code(r), "REFUSED")
        d = r["error"]["detail"]
        self.assertEqual((d["check"], d["table"], d["bucket"]), ("TURBO_ORDER", "P", 1))
        self.assertIn("not non-increasing", r["error"]["message"])
        self.assertIn("Nothing was sent", r["error"]["message"])

    def test_a_non_increasing_table_is_planned(self) -> None:
        h = self.boot_with(baseline=False, accepted_dir=self.accepted([57, 57, 56, 55, 55, 55, 55, 55]))
        r = h.call("t", "tune.plan", {"intent": {"restore": "accepted"}})
        self.assertTrue(r["ok"], r)

    def table(self, cpu: int = 0, msr: int = 0x1AD) -> list[int]:
        return [(self.sim.rdmsr(cpu, msr) >> (8 * k)) & 0xFF for k in range(8)]

    def test_an_intent_turbo_table_that_rises_is_refused_with_turbo_order(self) -> None:
        h = self.boot_with(baseline=False)
        self.read(h)
        for intent in ({"turbo": {"E": [48, 48, 49, 48, 48, 48, 48, 48]}}, {**rows(row("P1", curve=-10)), "turbo": {"P": [55, 57, 55, 55, 55, 55, 55, 55]}}):
            r = h.call(f"t{len(h.messages)}", "tune.plan", {"intent": intent})
            self.assertEqual(error_code(r), "REFUSED", r)
            self.assertEqual(r["error"]["detail"]["check"], "TURBO_ORDER")
        self.assertEqual(r["error"]["detail"]["table"], "P")

    def test_an_intent_turbo_table_above_the_policy_is_a_range_refusal(self) -> None:
        h = self.boot_with(baseline=False)
        self.read(h)  # "auto": the cap is the fused 0x1C value the read reported + 3 (D-038)
        r = h.call("t", "tune.plan", {"intent": {"turbo": {"P": [61] * 8}}})
        self.assertEqual((error_code(r), r["error"]["detail"]["check"]), ("REFUSED", "POLICY_RANGE"))

    def test_an_intent_turbo_table_before_any_read_is_left_to_the_guard(self) -> None:
        """Unread 0x1C = no raise: the plan-time range check has no number, the apply refuses (fail closed) -- nothing is sent above an unread cap."""
        h = self.boot_with(baseline=False)
        r = h.call("t", "tune.plan", {"intent": {"turbo": {"P": [60] * 8}}})
        self.assertTrue(r["ok"], r)

    def test_a_turbo_only_p_plan_applies_and_reads_back_without_any_mailbox_word(self) -> None:
        """G-117: ``{turbo: {P}}`` alone is a real plan: plain-MSR writes of 0x1AD, counts (0x1AE) untouched, no 0x14 / 0x15, no mailbox request."""
        h = self.boot_with(baseline=False)
        self.read(h)
        counts = self.sim.rdmsr(0, 0x1AE)
        p = self.plan(h, {"turbo": {"P": [57, 57, 56, 55, 55, 55, 55, 55]}})
        self.assertEqual((p["steps"], p["writes"], p["reads"], p["ops_needed"]), ([{"n": 1, "kind": "write_msr", "table": "p"}], 1, 0, 1))
        self.assertEqual((p["before"], p["after"], p["units"]), ({}, {}, []))
        self.assertTrue(any(w["code"] == "TURBO_RAISE" for w in p["warnings"]), p["warnings"])
        res = self.apply_ok(h, p)
        self.assertEqual([(s["kind"], s["table"], s["status"]) for s in res["steps"]], [("write_msr", "p", "ok")])
        self.assertEqual([(rb["unit"], rb["point"], rb["match"]) for rb in res["readback"]], [("P", "turbo", True)])
        self.assertEqual(res["writes_sent"], 1)
        self.assertEqual((res["msr_writes"], res["ops_used"]), (1, 1))  # G-119: the plain wrmsr is counted, and named apart
        self.assertEqual(self.table(), [57, 57, 56, 55, 55, 55, 55, 55])
        self.assertEqual(self.table(8, 0x650), [48] * 8)
        self.assertEqual(self.sim.rdmsr(0, 0x1AE), counts)
        self.assertEqual(self.sim.vf(0, 0, 0).to_u32(), 0x39)  # no per-core word was written

    def test_a_turbo_raise_without_any_read_0x1c_is_refused_by_the_guard(self) -> None:
        """Fail closed: no 0x1C read in the session (tune.read never ran) = no raise above the table's current bytes."""
        h = self.boot_with(baseline=False)
        p = self.plan(h, {"turbo": {"P": [57, 57, 56, 55, 55, 55, 55, 55]}})
        r = self.apply(h, p)
        self.assertEqual(error_code(r), "REFUSED", r)
        self.assertEqual(self.table(), [57, 57, 55, 55, 55, 55, 55, 55])

    def test_a_turbo_only_plan_still_predicts_vccia_from_the_new_table(self) -> None:
        h = self.boot_with()
        self.read(h)
        p = self.plan(h, {"turbo": {"P": [57, 57, 56, 55, 55, 55, 55, 55]}})
        v = p["vccia"]
        self.assertEqual((v["label"], v["active_cores"], v["margin_v"]), ("highest requested VID among units", "all", None))
        self.assertIsNotNone(v["before_v"])
        self.assertEqual(set(v["light_load"]), {"predicted_v", "set_by", "active_cores"})

    def test_a_turbo_only_e_plan_lowering_applies_and_reads_back(self) -> None:
        h = self.boot_with(baseline=False)
        self.read(h)
        p = self.plan(h, {"turbo": {"E": [48, 47, 46, 46, 46, 46, 46, 46]}})
        self.assertFalse(any(w["code"] == "TURBO_RAISE" for w in p["warnings"]))
        res = self.apply_ok(h, p)
        self.assertEqual([(rb["unit"], rb["match"]) for rb in res["readback"]], [("E", True)])
        self.assertEqual(self.table(8, 0x650), [48, 47, 46, 46, 46, 46, 46, 46])
        self.assertEqual(self.sim.rdmsr(0, 0x1AD), 0x3737373737373939)

    def test_a_turbo_only_plan_with_both_tables_writes_both(self) -> None:
        h = self.boot_with(baseline=False)
        self.read(h)
        p = self.plan(h, {"turbo": {"P": [57, 57, 56, 55, 55, 55, 55, 55], "E": [48, 47, 46, 46, 46, 46, 46, 46]}})
        self.assertEqual((p["writes"], p["ops_needed"]), (2, 2))
        res = self.apply_ok(h, p)
        self.assertEqual((res["msr_writes"], res["ops_used"]), (2, 2))
        self.assertEqual(sorted(rb["unit"] for rb in res["readback"]), ["E", "P"])
        self.assertEqual(self.table(), [57, 57, 56, 55, 55, 55, 55, 55])
        self.assertEqual(self.table(8, 0x650), [48, 47, 46, 46, 46, 46, 46, 46])

    def test_a_turbo_only_table_equal_to_the_current_one_plans_no_write(self) -> None:
        h = self.boot_with(baseline=False)
        self.read(h)
        p = self.plan(h, {"turbo": {"P": self.table()}})
        res = self.apply_ok(h, p)
        self.assertEqual((res["steps"], res["readback"], res["writes_sent"]), ([], [], 0))

    def test_a_turbo_only_order_violation_is_refused_before_anything_is_planned(self) -> None:
        h = self.boot_with(baseline=False)
        self.read(h)
        before = (self.sim.rdmsr(0, 0x1AD), self.sim.rdmsr(0, 0x650))
        for table in ("P", "E"):
            r = h.call(f"bad{table}", "tune.plan", {"intent": {"turbo": {table: [48, 49, 48, 48, 48, 48, 48, 48]}}})
            self.assertEqual((error_code(r), r["error"]["detail"]["check"], r["error"]["detail"]["table"]), ("REFUSED", "TURBO_ORDER", table))
        self.assertEqual((self.sim.rdmsr(0, 0x1AD), self.sim.rdmsr(0, 0x650)), before)

    def test_a_raise_is_flagged_with_its_code(self) -> None:
        h = self.boot_with(baseline=False)
        self.read(h)
        p = self.plan(h, {**rows(row("P1", curve=-10)), "turbo": {"P": [57, 57, 56, 55, 55, 55, 55, 55]}})
        self.assertTrue(any(w["code"] == "TURBO_RAISE" and "bucket(s) 3" in w["text"] for w in p["warnings"]), p["warnings"])
        self.apply_ok(h, p)
        self.assertEqual(self.table(), [57, 57, 56, 55, 55, 55, 55, 55])
        self.assertEqual(self.table(8, 0x650), [48] * 8)  # untouched

    def test_turbo_rides_with_rows(self) -> None:
        h = self.boot_with(baseline=False)
        self.read(h)
        p = self.plan(h, {**rows(row("P1", curve=-10)), "turbo": {"E": [48, 47, 46, 46, 46, 46, 46, 46]}})
        self.apply_ok(h, p)
        self.assertEqual(self.table(8, 0x650), [48, 47, 46, 46, 46, 46, 46, 46])
        self.assertFalse(any(w["code"] == "TURBO_RAISE" for w in p["warnings"]))


class WordsAssumedTests(FactsCase):
    def test_words_assumed_only_when_a_written_cpu_has_no_word_from_this_boot(self) -> None:
        """G-125: words that came from the read (``tune.last``) are not "assumed"; a plan on an unread CPU is."""
        h = self.boot_with()
        p = self.plan(h, rows(row("P0", curve=-10)))
        self.assertTrue(p["words_assumed"])  # nothing read yet: the OC word of P0 is assumed
        self.read(h)
        p = self.plan(h, rows(row("P0", curve=-10), row("EC1", curve=-10)))
        self.assertFalse(p["words_assumed"])
        p = self.plan(h, rows(row("P3", curve=-10)))
        self.assertFalse(p["words_assumed"])
