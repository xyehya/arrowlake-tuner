"""G-137 / D-039 through the helper: ``tune.read`` reads the CPU's own limits (``0x01`` ratio, ``0x28`` V target) and reports them as ``hw_limits``; ``tune.plan`` judges
ratio / V target / offset against the policy clamped by them and every refusal names its source. Simulator only; the policy file is a copy of ``policies/default.toml``
with the numbers each test needs.
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


class HwLimitsCase(TuneCase):
    def set_policy(self, **ratio_and_offsets: Any) -> None:
        """The shipped policy with ``ratio_max P/E`` numbers and optional top-level numbers replaced (``key=value`` lines)."""
        text = ADVANCED.read_text()
        text = text.replace('[ratio_max]\nP = "auto"\nE = "auto"', f'[ratio_max]\nP = {ratio_and_offsets.pop("p", 90)}\nE = {ratio_and_offsets.pop("e", 49)}')
        for key, value in ratio_and_offsets.items():
            old = next(line for line in text.splitlines() if line.startswith(f"{key} = "))
            text = text.replace(old, f"{key} = {value}")
        self.policy.write_text(text)

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

    def test_the_limits_come_from_one_p_cpu_and_one_e_cpu_one_transaction_each(self) -> None:
        h = self.boot()
        self.read(h)
        asked = [(r.cpu, r.cmd, r.p1, r.p2) for r in self.sim.requests if r.cmd in (0x01, 0x28)]
        self.assertEqual(asked, [(0, 0x01, 0, 0), (0, 0x28, 0, 0), (8, 0x01, 0, 0), (8, 0x28, 0, 0)])
        self.assertTrue(all(msr == 0x150 for _, msr, _ in self.sim.writes))  # every mailbox read is one WRMSR 0x150, nothing else

    def test_a_cpu_that_reports_no_ratio_overclocking_is_no_limit(self) -> None:
        h = self.boot(SimConfig.measured(fixed_reads={**measured_baseline.FIXED_READS, (0x01, 0, 0): 0x078}, per_point=True, reject_zero_fields=True, point_offsets={}))  # bits [10:8] clear
        res = self.read(h)
        self.assertIsNone(res["hw_limits"]["P"]["ratio_max_oc"])
        self.assertEqual(res["hw_limits"]["P"]["v_target_max_mv"], 1625)  # the other limit is unaffected

    def test_a_policy_without_the_read_entry_leaves_the_limits_unread_and_says_so(self) -> None:
        text = ADVANCED.read_text()
        start = text.index('name = "oc_capability_read"')
        start = text.rindex("[[allow]]", 0, start)
        self.policy.write_text(text[:start] + text[text.index("[[allow]]", start + 1):])
        h = self.boot()
        res = self.read(h)
        self.assertEqual((res["hw_limits"]["P"]["ratio_max_oc"], res["hw_limits"]["E"]["ratio_max_oc"]), (None, None))  # unknown: no raise
        self.assertEqual((res["hw_limits"]["P"]["v_target_max_mv"], res["hw_limits"]["E"]["v_target_max_mv"]), (1625, 1355))  # each read degrades alone
        self.assertIn("the CPU's ratio limit (0x01) not read", res["extras_notes"]["limits"])

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


class PlanJudgesAgainstThePolicyClampedByTheCpu(HwLimitsCase):
    def test_a_policy_of_90_is_enforced_up_to_the_cpu_limit_and_refused_above_it_with_the_source(self) -> None:
        self.set_policy(p=90)
        h = self.boot()
        self.read(h)
        plan = self.plan(h, rows(row("P0", ratio=62)))  # the CPU allows 120 and the policy 90: 62 is fine
        self.assertEqual(plan["units"], ["P0"])
        message = self.plan_error(h, rows(row("P0", ratio=91)))
        self.assertIn("ratio 91 is above policy ratio_max P = 90", message)
        self.assertIn(f"{self.policy}; your CPU allows up to 120x per cmd 0x01", message)  # the policy file and the CPU limit

    def test_a_policy_above_the_cpu_limit_loads_and_the_cpu_wins(self) -> None:
        self.set_policy(p=100)
        h = self.boot(cpu_reports(74))
        self.read(h)
        self.plan(h, rows(row("P0", ratio=74)))
        message = self.plan_error(h, rows(row("P0", ratio=75)))
        self.assertIn("ratio 75 is above CPU limit 74x (cmd 0x01 [7:0] fused max OC ratio", message)
        self.assertIn("policy ratio_max P = 100", message)

    def test_a_v_target_above_the_cpu_limit_names_cmd_0x28(self) -> None:
        self.set_policy(p=90)
        text = self.policy.read_text().replace('[v_target_max_mv]\nP = "auto"', "[v_target_max_mv]\nP = 1700")
        self.policy.write_text(text)
        h = self.boot()
        self.read(h)
        message = self.plan_error(h, rows(row("P0", v=1.65)))
        self.assertIn("OC-point voltage 1650 mV is above CPU limit 1625 mV (cmd 0x28 max - 1 LSB", message)
        self.assertIn("policy v_target_max_mv P = 1700 mV", message)

    def test_an_offset_past_the_shipped_window_is_accepted_when_the_policy_allows_it_and_refused_with_the_policy_as_source_when_not(self) -> None:
        h = self.boot()
        self.read(h)
        message = self.plan_error(h, rows(row("P0", curve=-500)))  # shipped window -200 .. +200 mV
        self.assertIn("effective_offset_floor_mv = -200", message)
        self.assertIn(str(self.policy), message)
        self.assertIn("field encoding allows -1000..999 mV", message)
        self.set_policy(effective_offset_floor_mv=-500)
        plan = self.plan(h, rows(row("P0", curve=-500)))
        self.assertEqual(plan["units"], ["P0"])
        self.set_policy(effective_offset_floor_mv=-500, offset_ceiling_mv=999)
        self.plan(h, rows(row("P0", curve=999)))


class ApplyUsesTheLiveLimits(HwLimitsCase):
    def test_a_ratio_raise_within_policy_and_cpu_limit_is_applied(self) -> None:
        self.set_policy(p=90)
        h = self.boot()
        self.read(h)
        self.apply_ok(h, self.plan(h, rows(row("P0", ratio=60))))
        self.assertTrue([q for q in self.sim.requests if q.cmd == 0x11])  # the limits read by tune.read bound the policy of the plan and of the apply session

    def test_when_the_limits_are_not_known_the_apply_reads_0x01_itself_first(self) -> None:
        self.set_policy(p=90)
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
        self.set_policy(p=90)
        h = self.boot()
        self.read(h)
        known = self.plan(h, rows(row("P0", ratio=60)))["ops_needed"]
        h.helper._tune_state.hw_limits = None
        self.assertEqual(self.plan(h, rows(row("P0", ratio=60)))["ops_needed"], known + 1)

    def test_a_policy_without_the_read_entry_refuses_the_plan_and_nothing_is_written(self) -> None:
        text = ADVANCED.read_text()
        start = text.index('name = "oc_capability_read"')
        start = text.rindex("[[allow]]", 0, start)
        text = text[:start] + text[text.index("[[allow]]", start + 1):]
        self.policy.write_text(text.replace('[ratio_max]\nP = "auto"\nE = "auto"', "[ratio_max]\nP = 90\nE = 49"))
        h = self.boot()
        self.read(h)
        r = h.call("plan9", "tune.plan", {"intent": rows(row("P0", ratio=60))})  # refused before anything is sent: the plan needs a read the policy does not allow
        self.assertFalse(r["ok"], r)
        self.assertIn("cmd 0x01", r["error"]["message"])
        self.assertEqual([q for q in self.sim.requests if q.cmd == 0x11], [])


class AutoCeilingsOfTheShippedPolicy(HwLimitsCase):
    """The public default is ``"auto"`` for P and E (ratio and V target): each resolves from what ``tune.read`` read for that kind."""

    def setUp(self) -> None:
        super().setUp()
        self.policy.write_text(ADVANCED.read_text())  # the shipped file as it is: no hand-written number

    def test_the_e_ratio_ceiling_is_resolved_from_the_0x1c_word_of_an_e_cluster_plus_3(self) -> None:
        h = self.boot()
        res = self.read(h)
        e_reads = [(r.cpu, r.cmd) for r in self.sim.requests if r.cmd == 0x1C and r.cpu >= 8]
        self.assertEqual(e_reads, [(8, 0x1C), (12, 0x1C), (16, 0x1C), (20, 0x1C)])  # one per E cluster, one transaction each
        self.assertEqual(res["limits"]["ratio_max"], {"P": 60, "E": 49})  # P 0x3939 -> fused 57 + 3; E 0x2E30 -> fused byte 46 + 3 (D-038; [7:0] 48 is the turbo setting) [MEASURED win 2026-10-01 / lnx 2026-10-11: 0x1C answers on E CPUs]
        self.assertEqual(res["limits"]["source"]["ratio_max"], {"P": "auto", "E": "auto"})
        self.assertNotIn("favored ratios", " ".join(res["extras_notes"].values()))
        self.assertEqual(sorted(res["turbo"]["favored"]), [str(c) for c in range(8)])  # the E words are not favored ratios

    def test_an_e_ratio_raise_is_planned_and_applied_under_auto(self) -> None:
        h = self.boot()
        self.read(h)
        self.assertIn("fused 46 + 3 = 49", self.plan_error(h, rows(row("EC0", ratio=50))))
        self.apply_ok(h, self.plan(h, rows(row("EC0", ratio=49))))

    def test_the_v_target_ceiling_is_the_live_0x28_value_not_a_constant(self) -> None:
        h = self.boot(SimConfig.measured(max_voltage_cpu={0: 0x700, **{c: 0x560 for c in range(8, 24)}}, per_point=True, reject_zero_fields=True, point_offsets={}))
        res = self.read(h)
        self.assertEqual(res["hw_limits"]["P"]["v_target_max_mv"], 1749)  # (0x700 - 1) / 1024 V in whole mV
        self.assertEqual(res["hw_limits"]["E"]["v_target_max_mv"], 1342)  # (0x560 - 1) / 1024 V
        self.assertEqual(res["limits"]["v_target_max_v"], {"P": 1.749, "E": 1.342})

    def test_the_v_target_ceiling_is_none_while_0x28_is_unread(self) -> None:
        h = self.boot()
        self.read(h)
        h.helper._tune_state.hw_limits = None
        from arltuner.helper import ops_tune

        lim = ops_tune._limits(ops_tune._load_policy(h.helper), ops_tune._topology(h.helper), h.helper._tune_state.favored_words)
        self.assertEqual(lim["v_target_max_v"], {"P": None, "E": None})
        self.assertEqual(lim["ratio_max"], {"P": None, "E": None})  # the ratio needs the CPU limit too
