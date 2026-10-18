"""T-022 / D-049 planner tests (pure: no port, no guard, no files): raise / lower words, the voltage-first / frequency-first split, the 0x28 read, pre-flight, E-cluster behaviour.

D-049 removed the per-operation step limits: a plan is always ONE round, ``NeedsRamp`` / ``next_profile_text`` never occur, and a big change is a warning
(``test_planner.Warnings``). The transition-safe order of the writes (voltage first when raising, frequency first when lowering) is a PROCESS rule and stays.

Facts: ``[MEASURED win 2026-10-01]`` win-xtu-live-trace.md (the XTU-shaped word, 0x28 per core); the ordering rule is registers.md "Ordering"
(``[DECOMPILED]``). Whether the hardware accepts a raise is ``[UNVERIFIED]``: nothing here claims it does.
"""
from __future__ import annotations

import dataclasses
import unittest

from arltuner.controls import (
    RAISE_CAVEAT,
    CoreReadings,
    PlanRefused,
    StepKind,
    build_plan,
    format_plan,
    preflight,
    read_steps,
    resolve_targets,
    xtu_word,
)
from arltuner.controls.planner import MAX_RAMP_ROUNDS, ramp_rounds
from arltuner.mailbox import VfWord
from arltuner.msr import measured_baseline as measured
from arltuner.topology import arl_s_285k

from ._support import e_core, p4, policy_with, profile

TOPO = arl_s_285k()
POLICY = policy_with()  # the CPU's ratio limit bound as 120x; nothing else
E_V = 0x4A0  # 1184 raw, fused V target of E cluster 0
P4_V = 0x544  # 1348 raw


def measured_readings(cpu: int, vf: int | None = None) -> CoreReadings:
    return CoreReadings(
        vf=VfWord.from_u32(measured.VF_DOMAIN0[cpu] if vf is None else vf),
        favored=measured.FAVORED_WORD[cpu],
        fused=measured.FUSED_POINT[cpu],
        granular=3,
        vmax=measured.MAX_VOLTAGE_RAW[cpu],
    )


def p4_readings(vf: VfWord = VfWord(55, P4_V, False, 0)) -> CoreReadings:
    return CoreReadings(vf=vf, favored=vf.ratio | vf.ratio << 8, fused=vf.ratio | vf.v_target_raw << 8, granular=3, vmax=0x681)


def e_plan(prof, policy=POLICY, cpus=(8, 9, 10, 11), vf=None):
    return build_plan(prof, TOPO, {c: measured_readings(c, vf) for c in cpus}, per_core_enabled=True, policy=policy)


def p4_plan(prof, vf: VfWord = VfWord(55, P4_V, False, 0), policy=POLICY):
    return build_plan(prof, TOPO, {4: p4_readings(vf)}, per_core_enabled=True, policy=policy)


def vf_steps(plan, cpu: int):
    return [s for s in plan.steps if s.kind is StepKind.WRITE_VF and s.cpu == cpu]


def w(ratio: int, v_raw: int, offset_raw: int = 0) -> VfWord:
    return VfWord(ratio, v_raw, False, offset_raw)


class TheTwoWordSplit(unittest.TestCase):
    def test_ratio_and_voltage_rising_go_voltage_first(self) -> None:
        plan = e_plan(profile(e_core(0, ratio=49, oc_voltage_mv=1176)))
        self.assertTrue(plan.allowed, plan.refusals)
        first, second = vf_steps(plan, 8)
        self.assertEqual((first.after, second.after), (w(48, E_V + 20), w(49, E_V + 20)))  # V target first, the OLD ratio kept
        self.assertEqual((first.stage, second.stage), ("1/2 voltage first", "2/2 full word"))
        self.assertTrue(first.verdict.allowed and second.verdict.allowed)
        self.assertEqual([s.number for s in (first, second)], [first.number, first.number + 1])  # consecutive, before the 0x1D
        order = [s.cmd for s in plan.steps if s.cpu == 8 and s.kind.is_write]
        self.assertEqual(order, [0x11, 0x11, 0x1D, 0x27])
        self.assertEqual(next(s for s in plan.steps if s.cpu == 8 and s.cmd == 0x1D).data, 49)  # the 0x1D follows the RAISED ratio

    def test_ratio_and_voltage_falling_go_frequency_first(self) -> None:
        plan = e_plan(profile(e_core(0, ratio=47, oc_voltage_mv=1137)))  # 48 -> 47, 1184 -> 1164 raw
        first, second = vf_steps(plan, 8)
        self.assertEqual((first.after, second.after), (w(47, E_V), w(47, 1164)))  # ratio first, the OLD V target kept
        self.assertEqual((first.stage, second.stage), ("1/2 frequency first", "2/2 full word"))
        self.assertTrue(plan.allowed, plan.refusals)

    def test_a_big_raise_is_still_two_words_not_many_rounds(self) -> None:
        plan = e_plan(profile(e_core(0, ratio=56, oc_voltage_mv=1300)))  # +8 bins, +116 mV: the old step limits made this 8 rounds
        self.assertTrue(plan.allowed, plan.refusals)
        first, second = vf_steps(plan, 8)
        self.assertEqual((first.stage, second.stage), ("1/2 voltage first", "2/2 full word"))
        self.assertEqual((first.after.ratio, first.after.v_target_raw), (48, VfWord.v_to_raw(1.3)))
        self.assertEqual((second.after.ratio, second.after.v_target_raw), (56, VfWord.v_to_raw(1.3)))

    def test_opposite_directions_and_single_changes_are_one_word(self) -> None:
        for kw in ({"ratio": 49}, {"oc_voltage_mv": 1176}, {"ratio": 47}, {"offset_mv": -10}, {"ratio": 49, "oc_voltage_mv": 1137}):
            with self.subTest(kw=kw):
                plan = e_plan(profile(e_core(0, **kw)))
                steps = vf_steps(plan, 8)
                self.assertEqual(len(steps), 1)
                self.assertEqual(steps[0].stage, "")
                self.assertTrue(plan.allowed, plan.refusals)

    def test_the_offset_moves_in_the_second_word(self) -> None:
        plan = e_plan(profile(e_core(0, ratio=49, oc_voltage_mv=1176, offset_mv=10)))
        first, second = vf_steps(plan, 8)
        self.assertEqual(first.after.offset_raw, 0)  # not before the ratio / voltage have moved
        self.assertEqual(second.after.offset_raw, 10)  # round(10 * 1.024) = 10

    def test_every_cpu_of_the_cluster_gets_the_same_two_words(self) -> None:
        plan = e_plan(profile(e_core(0, ratio=49, oc_voltage_mv=1176)))
        words = {tuple(s.data for s in vf_steps(plan, c)) for c in (8, 9, 10, 11)}
        self.assertEqual(len(words), 1)

    def test_the_step_count_and_budget_include_the_extra_write_and_its_read_back(self) -> None:
        single = e_plan(profile(e_core(0, ratio=49)))
        double = e_plan(profile(e_core(0, ratio=49, oc_voltage_mv=1176)))
        # per CPU: the 0x28 read and one more 0x11 (+ its read-back); four CPUs
        self.assertEqual(double.guard_operations - single.guard_operations, 4 * 2)
        self.assertEqual(double.required_operations - single.required_operations, 4 * 3)


class TheCpuLimitsDecideTheRaise(unittest.TestCase):
    """D-049: no policy ceiling and no step limit: a raise is judged by the CPU's own limits (cmd 0x01 ratio, cmd 0x28 voltage); unread = refused naming the read."""

    UNBOUND = policy_with(hw_ratio_max={"P": None, "E": None})

    def test_a_raise_within_the_cpu_limits_is_allowed(self) -> None:
        for kw in ({"ratio": 49}, {"ratio": 60}, {"oc_voltage_mv": 1176}, {"oc_voltage_mv": 1300}, {"ratio": 50, "oc_voltage_mv": 1180}):
            with self.subTest(kw=kw):
                plan = e_plan(profile(e_core(0, **kw)))
                self.assertTrue(plan.allowed, plan.refusals)

    def test_with_the_cpu_ratio_limit_unread_a_ratio_raise_is_predicted_refused_naming_the_read(self) -> None:
        plan = e_plan(profile(e_core(0, ratio=49)), policy=self.UNBOUND)
        self.assertFalse(plan.allowed)
        self.assertFalse(plan.needs_ramp)
        self.assertTrue(all("CPU_LIMIT_UNREAD" in r.detail and "cmd 0x01" in r.detail for r in plan.refusals if r.cmd == 0x11), plan.refusals)
        self.assertIn(StepKind.READ_OC_CAP, [s.kind for s in plan.steps])  # the plan reads the limit itself
        with self.assertRaises(PlanRefused):
            plan.require_allowed()

    def test_lowering_and_undervolting_never_need_a_limit(self) -> None:
        for kw in ({"offset_mv": -20}, {"ratio": 47}, {"oc_voltage_mv": 1150}, {"ratio": 47, "offset_mv": -20}):
            with self.subTest(kw=kw):
                self.assertTrue(e_plan(profile(e_core(0, **kw)), policy=self.UNBOUND).allowed)


class NoNextStep(unittest.TestCase):
    """Replaces the ``NeedsRamp`` / next-profile tests of D-016: the step limits are gone, a request is judged whole."""

    def test_a_ratio_two_bins_up_is_one_plan(self) -> None:
        plan = e_plan(profile(e_core(0, ratio=50)))
        self.assertTrue(plan.allowed, plan.refusals)
        self.assertFalse(plan.needs_ramp)
        self.assertIsNone(plan.next_profile_text)
        plan.require_allowed()

    def test_every_goal_is_one_allowed_plan(self) -> None:
        for ratio in (None, 45, 49, 50, 60):
            for mv in (None, 1100, 1177, 1215, 1300):
                for off in (None, -150, -80, 0, 25):
                    kw = {k: v for k, v in (("ratio", ratio), ("oc_voltage_mv", mv), ("offset_mv", off)) if v is not None}
                    if not kw:
                        continue
                    plan = e_plan(profile(e_core(0, **kw)))
                    self.assertTrue(plan.allowed, (kw, plan.refusals))
                    self.assertIsNone(plan.next_profile_text, kw)

    def test_lowering_is_one_plan_too(self) -> None:
        plan = e_plan(profile(e_core(0, ratio=45, offset_mv=-90)))
        self.assertTrue(plan.allowed, plan.refusals)
        self.assertIsNone(plan.next_profile_text)

    def test_ramp_rounds_is_always_one(self) -> None:
        readings = {c: measured_readings(c) for c in (8, 9, 10, 11)}
        self.assertEqual(ramp_rounds(profile(e_core(0, ratio=60, offset_mv=-300)), TOPO, readings, {}, POLICY), (1, None))
        self.assertGreaterEqual(MAX_RAMP_ROUNDS, 1)  # the bound stays for check_rounds

    def test_the_dry_run_text_has_no_next_step_section(self) -> None:
        text = format_plan(e_plan(profile(e_core(0, ratio=50))))
        self.assertNotIn("NEXT ALLOWED STEP", text)
        self.assertNotIn("per-operation limit", text)

    def test_an_absolute_cpu_bound_is_a_refusal_not_a_ramp(self) -> None:
        plan = e_plan(profile(e_core(0, ratio=121)))  # above cmd 0x01 = 120
        self.assertFalse(plan.needs_ramp)
        self.assertIsNone(plan.next_profile_text)
        self.assertTrue(any("cmd 0x01" in r.detail for r in plan.refusals))
        with self.assertRaises(PlanRefused):
            plan.require_allowed()

    def test_a_refused_target_does_not_hide_the_others(self) -> None:
        readings = {**{c: measured_readings(c) for c in (8, 9, 10, 11)}, 4: p4_readings()}
        plan = build_plan(profile(e_core(0, ratio=50), p4(ratio=121)), TOPO, readings, per_core_enabled=True, policy=POLICY)
        self.assertIsNone(plan.next_profile_text)
        refused_cpus = {r.cpu for r in plan.refusals}
        self.assertEqual(refused_cpus, {4})
        self.assertTrue(any("cmd 0x01" in r.detail for r in plan.refusals))


class ClusterEntries(unittest.TestCase):
    def test_cpus_of_a_cluster_that_disagree_are_called_out(self) -> None:
        readings = {c: measured_readings(c) for c in (8, 9, 10, 11)}
        readings[9] = measured_readings(9, VfWord(51, E_V, False, 0).to_u32())  # CPU 9 already sits at 51, the others at 48
        plan = build_plan(profile(e_core(0, ratio=50)), TOPO, readings, per_core_enabled=True, policy=POLICY)
        self.assertTrue(any("E cluster 0" in w_ and "different current V/F words" in w_ for w_ in plan.warnings), plan.warnings)
        self.assertTrue(plan.allowed, plan.refusals)
        self.assertIsNone(plan.next_profile_text)


class TheHardwareMaximumRead(unittest.TestCase):
    def test_0x28_is_read_only_where_the_voltage_can_rise(self) -> None:
        with_v = resolve_targets(profile(e_core(0, oc_voltage_mv=1176), p4(ratio=56)), TOPO)
        cmds = [(s.cpu, s.cmd) for s in read_steps(with_v, POLICY)]
        self.assertEqual([c for c in cmds if c[1] == 0x28], [(8, 0x28), (9, 0x28), (10, 0x28), (11, 0x28)])  # the E cluster, not P-Core 4
        none = resolve_targets(profile(e_core(0, ratio=49)), TOPO)
        self.assertNotIn(0x28, [s.cmd for s in read_steps(none, POLICY)])
        down = resolve_targets(profile(e_core(0, offset_mv=-20)), TOPO)
        self.assertNotIn(0x28, [s.cmd for s in read_steps(down, POLICY)])  # an undervolt cannot raise the voltage
        up = resolve_targets(profile(e_core(0, offset_mv=20)), TOPO)
        self.assertIn(0x28, [s.cmd for s in read_steps(up, POLICY)])  # D-049: a positive whole-curve offset is predicted against the CPU's voltage limit

    def test_a_plan_without_the_0x28_word_refuses_a_raised_v_target(self) -> None:
        readings = {c: dataclasses.replace(measured_readings(c), vmax=None) for c in (8, 9, 10, 11)}
        plan = build_plan(profile(e_core(0, oc_voltage_mv=1176)), TOPO, readings, per_core_enabled=True, policy=POLICY)
        self.assertFalse(plan.allowed)
        self.assertTrue(any("CPU_LIMIT_UNREAD" in r.detail and "cmd 0x28" in r.detail for r in plan.refusals))

    def test_the_plan_refuses_above_the_hardware_maximum(self) -> None:
        readings = {c: dataclasses.replace(measured_readings(c), vmax=0x4B0) for c in (8, 9, 10, 11)}  # hardware max 0x4AF = 1199 raw
        plan = build_plan(profile(e_core(0, oc_voltage_mv=1200)), TOPO, readings, per_core_enabled=True, policy=POLICY)
        self.assertTrue(any("CPU limit" in r.detail and "cmd 0x28" in r.detail for r in plan.refusals))
        self.assertFalse(plan.needs_ramp)


class PreflightHasNoPolicyBounds(unittest.TestCase):
    def test_values_the_old_policy_refused_pass_preflight(self) -> None:
        for prof in (
            profile(e_core(0, ratio=51)),
            profile(p4(ratio=59)),
            profile(e_core(0, oc_voltage_mv=1251)),
            profile(p4(oc_voltage_mv=1501)),
            profile(e_core(0, offset_mv=26)),
            profile(p4(offset_mv=-51)),
            profile(p4(offset_mv=-400)),
        ):
            with self.subTest(prof=prof.entries[0]):
                preflight(prof, TOPO, policy_with(max_ops=400))

    def test_the_allowlist_still_refuses_before_any_read(self) -> None:
        policy = dataclasses.replace(policy_with(max_ops=400), cpus=frozenset(range(4)))
        with self.assertRaises(PlanRefused) as ctx:
            preflight(profile(p4(offset_mv=-20)), TOPO, policy)
        self.assertTrue(all(r.reason == "CPU_NOT_ALLOWED" for r in ctx.exception.refusals))

    def test_the_count_of_a_raise_profile_is_the_worst_case_with_every_extra(self) -> None:
        from arltuner.controls import NeedsHigherLimit

        prof = profile(e_core(0, ratio=49, oc_voltage_mv=1176))
        preflight(prof, TOPO, policy_with(max_ops=55))
        with self.assertRaises(NeedsHigherLimit) as ctx:
            preflight(prof, TOPO, policy_with(max_ops=54))
        self.assertEqual(ctx.exception.required, 55)  # 0x14 + 4 x (5 reads + 4 writes) + 0x15 = 38 operations + 17 read-backs

    def test_an_unbound_policy_adds_the_0x01_read_to_the_worst_case(self) -> None:
        from arltuner.controls import NeedsHigherLimit

        prof = profile(e_core(0, ratio=49, oc_voltage_mv=1176))
        unbound = policy_with(hw_ratio_max={"P": None, "E": None})
        with self.assertRaises(NeedsHigherLimit) as ctx:
            preflight(prof, TOPO, dataclasses.replace(unbound, max_operations_per_session=55))
        self.assertEqual(ctx.exception.required, 56)  # one more mailbox read: the CPU's own ratio limit of the E kind


class TheCurrentStateIsKept(unittest.TestCase):
    def test_xtu_word_keeps_a_v_target_the_core_already_carries(self) -> None:
        # D-016: a V target set earlier is NOT reset to the fused voltage
        self.assertEqual(xtu_word(w(49, E_V + 20), 0x2E31, 0x4A02E).to_u32(), w(49, E_V + 20).to_u32())
        self.assertEqual(xtu_word(w(0, 0), 0x2E30, 0x4A02E), w(48, E_V))  # a zero field is still filled in as XTU does
        self.assertEqual(xtu_word(w(0, 0x4B4, offset_raw=-5), 0x2E30, 0x4A02E), w(48, 0x4B4, -5))  # the baseline V target and offset stay

    def test_a_ratio_only_step_after_a_raised_voltage_does_not_lower_the_voltage(self) -> None:
        raised = VfWord(49, E_V + 20, False, 0)
        plan = e_plan(profile(e_core(0, ratio=50)), vf=raised.to_u32())
        (word_,) = vf_steps(plan, 8)
        self.assertEqual(word_.after, w(50, E_V + 20))  # the V target of the baseline is carried over
        self.assertTrue(plan.allowed, plan.refusals)

    def test_the_raise_caveat_is_only_on_plans_that_raise(self) -> None:
        self.assertIn(RAISE_CAVEAT, e_plan(profile(e_core(0, ratio=49))).warnings)
        self.assertNotIn(RAISE_CAVEAT, e_plan(profile(e_core(0, ratio=47, offset_mv=-10))).warnings)
        self.assertTrue(RAISE_CAVEAT.isascii())
        self.assertIn("UNVERIFIED", RAISE_CAVEAT)
        self.assertIn("reboot", RAISE_CAVEAT)
        self.assertNotIn("reviewed policy", RAISE_CAVEAT)  # D-049: no policy numbers bound a raise any more


if __name__ == "__main__":
    unittest.main()
