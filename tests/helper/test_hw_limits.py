"""G-137 / D-039 / D-049 through the helper: ``tune.read`` reads the CPU's own limits (``0x01`` ratio, ``0x28`` V target) and reports them as ``hw_limits`` and ``cpu_limits``;
``tune.plan`` judges ratio / V target / offset against them (the only value limits) and every refusal names its source. Simulator only; the policy file is a copy of
``policies/default.toml``.
"""
from __future__ import annotations

from typing import Any

from arltuner.msr import SimConfig, measured_baseline

from .test_tune import ADVANCED, TuneCase, row, rows

LIMITS_P = {"ratio_max_oc": 120, "v_target_max_mv": 1625, "offset_mv": [-1000, 999]}  # 0x778, 0x681 [MEASURED win 2026-10-01]
LIMITS_E = {"ratio_max_oc": 120, "v_target_max_mv": 1355, "offset_mv": [-1000, 999]}  # 0x778 (E answers P1=0 [UNVERIFIED]), 0x56D


def cpu_reports(ratio: int) -> SimConfig:
    """The measured machine whose ``0x01 P1=0`` answers ``ratio`` (instead of the reference 120x)."""
    return SimConfig.measured(fixed_reads={**measured_baseline.FIXED_READS, (0x01, 0, 0): ratio | 0x700}, per_point=True, reject_zero_fields=True, point_offsets={})


def without_the_read_entry() -> str:
    text = ADVANCED.read_text()
    start = text.index('name = "oc_capability_read"')
    start = text.rindex("[[allow]]", 0, start)
    return text[:start] + text[text.index("[[allow]]", start + 1):]


class HwLimitsCase(TuneCase):
    def plan_error(self, h: Any, intent: dict[str, Any]) -> str:
        r = h.call(f"plan{len(h.messages)}", "tune.plan", {"intent": intent})
        self.assertFalse(r["ok"], r)
        self.assertEqual(r["error"]["detail"]["check"], "POLICY_RANGE", r)
        return r["error"]["message"]


class ReadsTheCpuLimits(HwLimitsCase):
    def test_tune_read_reports_hw_limits_per_kind(self) -> None:
        h = self.boot()
        res = self.read(h)
        self.assertEqual(res["hw_limits"], {"P": LIMITS_P, "E": LIMITS_E})
        self.assertNotIn("limits", res["extras_notes"])

    def test_the_limits_come_from_one_p_cpu_one_e_cpu_and_the_ring_one_transaction_each(self) -> None:
        h = self.boot()
        self.read(h)
        asked = [(r.cpu, r.cmd, r.p1, r.p2) for r in self.sim.requests if r.cmd in (0x01, 0x28)]
        self.assertEqual(asked, [(0, 0x01, 0, 0), (0, 0x28, 0, 0), (8, 0x01, 0, 0), (8, 0x28, 0, 0), (0, 0x01, 2, 0), (0, 0x28, 2, 0)])  # P, E, then the ring (P1=2) through the first P CPU
        self.assertTrue(all(msr == 0x150 for _, msr, _ in self.sim.writes))  # every mailbox read is one WRMSR 0x150, nothing else

    def test_a_cpu_that_reports_no_ratio_overclocking_is_no_limit(self) -> None:
        h = self.boot(SimConfig.measured(fixed_reads={**measured_baseline.FIXED_READS, (0x01, 0, 0): 0x078}, per_point=True, reject_zero_fields=True, point_offsets={}))  # bits [10:8] clear
        res = self.read(h)
        self.assertIsNone(res["hw_limits"]["P"]["ratio_max_oc"])
        self.assertEqual(res["hw_limits"]["P"]["v_target_max_mv"], 1625)  # the other limit is unaffected

    def test_a_policy_without_the_read_entry_leaves_the_limits_unread_and_says_so(self) -> None:
        self.policy.write_text(without_the_read_entry())
        h = self.boot()
        res = self.read(h)
        self.assertEqual((res["hw_limits"]["P"]["ratio_max_oc"], res["hw_limits"]["E"]["ratio_max_oc"]), (None, None))  # unknown: no raise
        self.assertEqual((res["hw_limits"]["P"]["v_target_max_mv"], res["hw_limits"]["E"]["v_target_max_mv"]), (1625, 1355))  # each read degrades alone
        self.assertIn("the CPU's ratio limit (0x01) not read", res["extras_notes"]["limits"])
        self.assertIsNone(res["cpu_limits"]["P"]["max_ratio"])  # ... and cpu_limits says null, not a number

    def test_tune_last_returns_them_with_the_stored_read(self) -> None:
        h = self.boot()
        self.read(h)
        r = h.call("last1", "tune.last", {})
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["result"]["result"]["hw_limits"], {"P": LIMITS_P, "E": LIMITS_E})

    def test_a_restarted_helper_gets_the_limits_back_from_the_stored_read(self) -> None:
        h = self.boot()
        self.read(h)
        h2 = self.boot()  # a new helper process over the same state directory: no tune.read yet
        policy = __import__("arltuner.helper.ops_tune", fromlist=["_load_policy"])._load_policy(h2.helper)
        self.assertEqual((dict(policy.hw_ratio_max), dict(policy.hw_v_target_max_mv)), ({"P": 120, "E": 120}, {"P": 1625, "E": 1355}))


class CpuLimitsDocument(HwLimitsCase):
    """D-049 ``cpu_limits``: everything the helper enforces, read at runtime, with the register it came from; ``null`` = not read."""

    def test_tune_read_returns_cpu_limits_with_their_sources(self) -> None:
        h = self.boot()
        res = self.read(h)
        lim = res["cpu_limits"]
        self.assertEqual((lim["P"]["max_mv"], lim["P"]["max_ratio"]), (1625, 120))
        self.assertEqual((lim["E"]["max_mv"], lim["E"]["max_ratio"]), (1355, 120))
        self.assertEqual(set(lim["ring"]), {"max_mv", "max_ratio", "source"})
        for kind in ("P", "E"):
            self.assertTrue(lim[kind]["source"].startswith("0x28/0x01 read"), lim[kind]["source"])
        self.assertEqual(lim["offset_mv"], [-1000, 999])
        self.assertEqual(set(lim["power"]), {"locked", "pl_max_w", "source"})  # not read by tune.read: nulls and a source saying so
        self.assertIsNone(lim["power"]["locked"])
        self.assertIsNone(lim["caps_khz"])
        self.assertIsNone(lim["uncore_khz"])

    def test_tune_last_returns_the_same_cpu_limits(self) -> None:
        h = self.boot()
        res = self.read(h)
        r = h.call("last2", "tune.last", {})
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["result"]["result"]["cpu_limits"], res["cpu_limits"])

    def test_the_cpu_limits_survive_a_helper_restart(self) -> None:
        h = self.boot()
        res = self.read(h)
        h2 = self.boot()
        r = h2.call("last3", "tune.last", {})
        self.assertTrue(r["ok"], r)
        got = r["result"]["result"]["cpu_limits"]
        for kind in ("P", "E", "ring"):
            self.assertEqual((got[kind]["max_mv"], got[kind]["max_ratio"]), (res["cpu_limits"][kind]["max_mv"], res["cpu_limits"][kind]["max_ratio"]), kind)

    def test_before_any_read_the_limits_are_null_and_say_not_read(self) -> None:
        from arltuner.helper import ops_tune

        h = self.boot()
        doc = ops_tune.cpu_limits_doc(h.helper)
        for kind in ("P", "E", "ring"):
            self.assertEqual((doc[kind]["max_mv"], doc[kind]["max_ratio"]), (None, None))
            self.assertIn("not read", doc[kind]["source"])

    def test_the_v_target_limit_is_the_live_0x28_value_not_a_constant(self) -> None:
        h = self.boot(SimConfig.measured(max_voltage_cpu={0: 0x700, **{c: 0x560 for c in range(8, 24)}}, per_point=True, reject_zero_fields=True, point_offsets={}))
        res = self.read(h)
        self.assertEqual(res["hw_limits"]["P"]["v_target_max_mv"], 1749)  # (0x700 - 1) / 1024 V in whole mV
        self.assertEqual(res["hw_limits"]["E"]["v_target_max_mv"], 1342)  # (0x560 - 1) / 1024 V
        self.assertEqual((res["cpu_limits"]["P"]["max_mv"], res["cpu_limits"]["E"]["max_mv"]), (1749, 1342))

    def test_field_zero_of_0x28_means_2_volts(self) -> None:
        h = self.boot(SimConfig.measured(max_voltage_cpu={0: 0x000, **{c: 0x560 for c in range(8, 24)}}, per_point=True, reject_zero_fields=True, point_offsets={}))
        res = self.read(h)
        self.assertEqual(res["cpu_limits"]["P"]["max_mv"], 2000)  # one meaning for the plan and for the guard (registers.md: the 12-bit field wraps, 0 = 2.0 V)

    def test_the_page_limits_carry_no_operator_value_limit_any_more(self) -> None:
        h = self.boot()
        res = self.read(h)
        self.assertEqual(res["limits"]["ratio_max"], {"P": None, "E": None})
        self.assertEqual(res["limits"]["v_target_max_v"], {"P": None, "E": None})
        self.assertEqual(res["limits"]["source"]["ratio_max"], {"P": "none", "E": "none"})
        self.assertEqual(res["limits"]["offset_mv"]["P"], {"min": -1000, "max": 999})


class PlanJudgesAgainstTheCpuLimits(HwLimitsCase):
    """D-049: the CPU's own limits (read by tune.read) are the only value limits; each refusal names the register it comes from."""

    def test_a_ratio_is_planned_up_to_the_cpu_limit_and_refused_above_it_with_the_source(self) -> None:
        h = self.boot(cpu_reports(74))
        self.read(h)
        self.plan(h, rows(row("P0", ratio=74)))
        message = self.plan_error(h, rows(row("P0", ratio=75)))
        self.assertIn("ratio 75 is above CPU limit 74x (cmd 0x01 [7:0] fused max OC ratio", message)

    def test_the_removed_policy_ratio_max_is_gone(self) -> None:
        h = self.boot()
        self.read(h)
        self.plan(h, rows(row("P0", ratio=62)))  # a policy "ratio_max P = 90" used to refuse above 90; the CPU allows 120
        self.plan(h, rows(row("P0", ratio=100)))

    def test_a_v_target_above_the_cpu_limit_names_cmd_0x28(self) -> None:
        h = self.boot()
        self.read(h)
        message = self.plan_error(h, rows(row("P0", v=1.65)))
        self.assertIn("OC-point voltage 1650 mV is above CPU limit 1625 mV (cmd 0x28 [11:0] - 1 LSB", message)

    def test_an_offset_is_bounded_only_by_the_field_encoding(self) -> None:
        h = self.boot()
        self.read(h)
        for mv in (-500, -1000, 999):
            self.assertEqual(self.plan(h, rows(row("P0", curve=mv)))["units"], ["P0"], mv)  # the old -200 .. +200 window is gone
        message = self.plan_error(h, rows(row("P0", curve=-1001)))
        self.assertIn("does not fit the 11-bit offset field", message)
        self.assertIn("-1000..999", message)


class ApplyUsesTheLiveLimits(HwLimitsCase):
    def test_a_ratio_raise_within_the_cpu_limit_is_applied(self) -> None:
        h = self.boot()
        self.read(h)
        self.apply_ok(h, self.plan(h, rows(row("P0", ratio=60))))
        self.assertTrue([q for q in self.sim.requests if q.cmd == 0x11])  # the limits read by tune.read bound the policy of the plan and of the apply session

    def test_when_the_limits_are_not_known_the_apply_reads_0x01_itself_first(self) -> None:
        h = self.boot()
        self.read(h)
        h.helper._tune_state.hw_limits = None  # a state in which the CPU limits are not known (a read that could not get them)
        plan = self.plan(h, rows(row("P0", ratio=60)))
        before = len(self.sim.requests)
        self.apply_ok(h, plan)
        asked = [(q.cmd, q.cpu) for q in self.sim.requests[before:]]
        self.assertEqual(asked.index((0x01, 0)), asked.index((0x26, 0)) + 1)  # the 0x01 read ends the read phase, before the first write
        self.assertLess(asked.index((0x01, 0)), asked.index((0x11, 0)))
        self.assertEqual(sum(1 for q in asked if q[0] == 0x01), 1)  # one per kind in the plan

    def test_the_budget_includes_the_0x01_read_only_while_the_limit_is_unknown(self) -> None:
        h = self.boot()
        self.read(h)
        known = self.plan(h, rows(row("P0", ratio=60)))["ops_needed"]
        h.helper._tune_state.hw_limits = None
        self.assertEqual(self.plan(h, rows(row("P0", ratio=60)))["ops_needed"], known + 1)

    def test_a_policy_without_the_read_entry_refuses_the_plan_and_nothing_is_written(self) -> None:
        self.policy.write_text(without_the_read_entry())
        h = self.boot()
        self.read(h)
        r = h.call("plan9", "tune.plan", {"intent": rows(row("P0", ratio=60))})  # refused before anything is sent: the plan needs a read the policy does not allow
        self.assertFalse(r["ok"], r)
        self.assertIn("cmd 0x01", r["error"]["message"])
        self.assertEqual([q for q in self.sim.requests if q.cmd == 0x11], [])

    def test_a_lowering_needs_no_ratio_limit(self) -> None:
        self.policy.write_text(without_the_read_entry())
        h = self.boot()
        self.read(h)
        self.plan(h, rows(row("P0", curve=-30)))  # an offset only: nothing is raised, nothing needs the unread limit


class TheEcoreRatioLimitIsTheCpusOwn(HwLimitsCase):
    """The shipped policy has no E ratio number any more (it used to be ``"auto"`` = 0x1C fused + 3): the E raise is judged against ``0x01`` like any other kind."""

    def test_an_e_ratio_raise_is_planned_and_applied(self) -> None:
        h = self.boot()
        self.read(h)
        self.apply_ok(h, self.plan(h, rows(row("EC0", ratio=49))))

    def test_an_e_ratio_above_the_cpu_limit_is_refused_with_the_source(self) -> None:
        h = self.boot(cpu_reports(49))
        self.read(h)
        self.plan(h, rows(row("EC0", ratio=49)))
        self.assertIn("cmd 0x01", self.plan_error(h, rows(row("EC0", ratio=50))))
