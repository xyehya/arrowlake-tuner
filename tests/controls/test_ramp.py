"""T-022 planner tests (pure: no port, no guard, no files): raise / lower words, the voltage-first / frequency-first split, step limits and the
next allowed step (``NeedsRamp``), the 0x28 read, pre-flight against explicit bounds, E-cluster behaviour.

Facts: ``[MEASURED win 2026-10-01]`` win-xtu-live-trace.md (the XTU-shaped word, 0x28 per core); the ordering rule is registers.md "Ordering"
(``[DECOMPILED]``). Whether the hardware accepts a raise is ``[UNVERIFIED]``: nothing here claims it does.
"""
from __future__ import annotations

import dataclasses
import unittest

from arltuner.controls import (
    RAISE_CAVEAT,
    CoreReadings,
    NeedsRamp,
    PlanRefused,
    StepKind,
    build_plan,
    format_plan,
    preflight,
    read_steps,
    resolve_targets,
    xtu_word,
)
from arltuner.mailbox import VfWord
from arltuner.msr import measured_baseline as measured
from arltuner.profiles import load_profile
from arltuner.topology import arl_s_285k

from ._support import DEFAULT_STEPS, e_core, p4, policy_with, profile

TOPO = arl_s_285k()
RAISE = dict(ratio_max={"E": 50, "P": 58}, v_target_max_mv={"E": 1250, "P": 1500}, offset_ceiling_mv=25)
POLICY = policy_with(**{**DEFAULT_STEPS, **RAISE})
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


class TheNoRaiseDefaultsOfTheShippedPolicy(unittest.TestCase):
    SHIPPED = policy_with(**DEFAULT_STEPS)  # relaxed nothing: no ratio / V ceilings, offset ceiling 0, default steps

    def test_every_raise_is_predicted_refused_after_the_reads(self) -> None:
        for kw in ({"ratio": 49}, {"oc_voltage_mv": 1176}, {"ratio": 50, "oc_voltage_mv": 1180}):
            with self.subTest(kw=kw):
                plan = e_plan(profile(e_core(0, **kw)), policy=self.SHIPPED)
                self.assertFalse(plan.allowed)
                self.assertFalse(plan.needs_ramp)  # not a step problem: no ramp is offered towards a forbidden value
                self.assertTrue(all("no-raise value" in r.detail for r in plan.refusals if r.cmd == 0x11), plan.refusals)
                with self.assertRaises(PlanRefused):
                    plan.require_allowed()

    def test_an_undervolt_offset_and_lowering_are_allowed(self) -> None:
        for kw in ({"offset_mv": -20}, {"ratio": 47}, {"oc_voltage_mv": 1150}, {"ratio": 47, "offset_mv": -20}):
            with self.subTest(kw=kw):
                self.assertTrue(e_plan(profile(e_core(0, **kw)), policy=self.SHIPPED).allowed)


class NextAllowedStep(unittest.TestCase):
    def test_a_ratio_two_bins_up_is_refused_with_the_exact_next_step(self) -> None:
        plan = e_plan(profile(e_core(0, ratio=50)))
        self.assertFalse(plan.allowed)
        self.assertTrue(plan.needs_ramp)
        self.assertEqual(
            plan.next_profile_text,
            '[meta]\nname = "t (next step)"\nplatform = "arl-s-285k"\n\n[[core]]\nkind = "E"\nindex = 0\nratio = 49\n',
        )
        with self.assertRaises(NeedsRamp) as ctx:
            plan.require_allowed()
        self.assertEqual(ctx.exception.next_profile_text, plan.next_profile_text)
        self.assertIn("ratio = 49", str(ctx.exception))

    def test_only_the_field_that_exceeds_is_moved_the_others_keep_the_requested_value(self) -> None:
        plan = e_plan(profile(e_core(0, ratio=49, oc_voltage_mv=1200, offset_mv=-10)))  # V +45 raw is too much; the ratio and offset are fine
        (entry,) = load_profile(plan.next_profile_text).entries
        self.assertEqual((entry.ratio, entry.oc_voltage_mv, entry.offset_mv), (49, 1176, -10))

    def test_a_voltage_step_is_expressed_in_whole_mv_that_never_pass_the_limit(self) -> None:
        # one mV is 1.024 raw: the mV value written in the next profile must convert to AT MOST the step limit
        for goal_mv in range(1160, 1260, 3):
            plan = e_plan(profile(e_core(0, oc_voltage_mv=goal_mv)))
            if not plan.needs_ramp:
                continue
            nxt = load_profile(plan.next_profile_text).entries[0].oc_voltage_mv
            raw = VfWord.v_to_raw(nxt / 1000)
            self.assertTrue(E_V < raw <= E_V + 20, (goal_mv, nxt, raw))
            self.assertTrue(e_plan(load_profile(plan.next_profile_text)).allowed, goal_mv)

    def test_mv_toward_is_the_nearest_whole_mv_that_does_not_pass_the_goal(self) -> None:
        # exhaustive over the V target range: one mV is 1.024 raw, so rounding to the nearest mV can land one raw past the goal
        from arltuner.controls.planner import _mv_toward

        def to_v(mv: int) -> int:
            return VfWord.v_to_raw(mv / 1000)

        for goal in range(900, 1700):
            up = _mv_toward(goal, goal - 20, to_v)  # rising: the greatest mV whose raw value is <= the goal
            self.assertTrue(to_v(up) <= goal < to_v(up + 1), (goal, up))
            down = _mv_toward(goal, goal + 20, to_v)  # falling: the smallest mV whose raw value is >= the goal
            self.assertTrue(to_v(down - 1) < goal <= to_v(down), (goal, down))
        for goal in range(-200, 201):  # the offset field, both signs
            up = _mv_toward(goal, goal - 20, VfWord.mv_to_offset_raw)
            self.assertTrue(VfWord.mv_to_offset_raw(up) <= goal < VfWord.mv_to_offset_raw(up + 1), (goal, up))
            down = _mv_toward(goal, goal + 20, VfWord.mv_to_offset_raw)
            self.assertTrue(VfWord.mv_to_offset_raw(down - 1) < goal <= VfWord.mv_to_offset_raw(down), (goal, down))

    def test_lowering_steps_down_too(self) -> None:
        plan = e_plan(profile(e_core(0, ratio=45, offset_mv=-90)))  # ratio -3, offset -90 mV: both too far
        self.assertTrue(plan.needs_ramp)
        (entry,) = load_profile(plan.next_profile_text).entries
        self.assertEqual((entry.ratio, entry.offset_mv), (47, -25))  # one bin / 25 mV toward the goal

    def test_a_request_inside_the_limits_has_no_next_step(self) -> None:
        plan = e_plan(profile(e_core(0, ratio=49)))
        self.assertTrue(plan.allowed)
        self.assertIsNone(plan.next_profile_text)
        plan.require_allowed()

    def test_an_absolute_bound_is_a_refusal_not_a_ramp(self) -> None:
        plan = e_plan(profile(e_core(0, ratio=55)))  # above ratio_max 50: stepping towards it would be a trap
        self.assertFalse(plan.needs_ramp)
        self.assertTrue(any("ratio_max" in r.detail for r in plan.refusals))
        with self.assertRaises(PlanRefused):
            plan.require_allowed()

    def test_no_next_step_is_offered_while_another_target_is_refused_for_good(self) -> None:
        # cluster 0 needs a ramp, P-Core 4 asks for a ratio above its ceiling: walking cluster 0 up would end in the refusal of P-Core 4
        readings = {**{c: measured_readings(c) for c in (8, 9, 10, 11)}, 4: p4_readings()}
        plan = build_plan(profile(e_core(0, ratio=50), p4(ratio=59)), TOPO, readings, per_core_enabled=True, policy=POLICY)
        self.assertFalse(plan.needs_ramp)
        self.assertIsNone(plan.next_profile_text)
        self.assertTrue(any("ratio_max" in r.detail for r in plan.refusals))
        with self.assertRaises(PlanRefused):
            plan.require_allowed()
        # the same profile without the forbidden request does get its next step
        plan = build_plan(profile(e_core(0, ratio=50), p4(ratio=56)), TOPO, readings, per_core_enabled=True, policy=POLICY)
        self.assertTrue(plan.needs_ramp)
        entries = load_profile(plan.next_profile_text).entries
        self.assertEqual([(e.kind, e.index, e.ratio) for e in entries], [("E", 0, 49), ("P", 4, 56)])  # P-Core 4's step is within the limit

    def test_the_dry_run_text_names_the_step_and_the_verdict(self) -> None:
        text = format_plan(e_plan(profile(e_core(0, ratio=50))))
        self.assertIn("NEXT ALLOWED STEP", text)
        self.assertIn("ratio = 49", text)
        self.assertIn("needs a smaller step", text)
        self.assertIn("exceeds the per-operation limit", text)  # the refused word shows the guard's reason

    def test_every_next_step_is_allowed_and_walking_converges(self) -> None:
        # property: for goals within the ceilings, (1) the next profile of a refused request is itself allowed, (2) repeating the walk reaches the goal
        policy = policy_with(**{**DEFAULT_STEPS, **RAISE})
        cases = [
            (ratio, mv, off)
            for ratio in (None, 45, 49, 50)
            for mv in (None, 1100, 1177, 1215, 1250)
            for off in (None, -80, -25, 0, 25)
            if (ratio, mv, off) != (None, None, None)
        ]
        for ratio, mv, off in cases:
            kw = {k: v for k, v in (("ratio", ratio), ("oc_voltage_mv", mv), ("offset_mv", off)) if v is not None}
            vf = VfWord(0, 0, False, 0)
            goal = profile(e_core(0, **kw))
            for step in range(12):
                plan = build_plan(goal, TOPO, {c: measured_readings(c, vf.to_u32()) for c in (8, 9, 10, 11)}, per_core_enabled=True, policy=policy)
                if plan.next_profile_text is None:
                    self.assertTrue(plan.allowed or plan.refusals, (kw, step))
                    break
                nxt = load_profile(plan.next_profile_text)
                nplan = build_plan(nxt, TOPO, {c: measured_readings(c, vf.to_u32()) for c in (8, 9, 10, 11)}, per_core_enabled=True, policy=policy)
                self.assertTrue(nplan.allowed, (kw, step, nplan.refusals))  # (1)
                vf = vf_steps(nplan, 8)[-1].after
                vf = dataclasses.replace(vf)  # the new current state: the last 0x11 word of that step
            else:
                self.fail(f"no convergence for {kw}")
            if plan.allowed:  # (2) the goal itself is now one allowed step away
                final = vf_steps(plan, 8)[-1].after
                if "ratio" in kw:
                    self.assertEqual(final.ratio, kw["ratio"], kw)
                if "oc_voltage_mv" in kw:
                    # G-124: ratio <= the E top fused ratio (46) = no OC point: the word carries no target, whatever the offset
                    dropped = final.ratio <= 46
                    self.assertEqual(final.v_target_raw, 0 if dropped else VfWord.v_to_raw(kw["oc_voltage_mv"] / 1000), kw)


class ClusterEntries(unittest.TestCase):
    def test_cpus_of_a_cluster_that_disagree_are_called_out_and_the_ramp_comes_from_the_first_that_needs_one(self) -> None:
        readings = {c: measured_readings(c) for c in (8, 9, 10, 11)}
        readings[9] = measured_readings(9, VfWord(51, E_V, False, 0).to_u32())  # CPU 9 already sits at 51, the others at 48
        plan = build_plan(profile(e_core(0, ratio=50)), TOPO, readings, per_core_enabled=True, policy=policy_with(**{**DEFAULT_STEPS, **RAISE}))
        self.assertTrue(any("E cluster 0" in w_ and "different current V/F words" in w_ for w_ in plan.warnings), plan.warnings)
        self.assertTrue(plan.needs_ramp)  # CPU 8 and 10, 11 must climb 48 -> 50; CPU 9 must come down 51 -> 50 (inside the step)
        (entry,) = load_profile(plan.next_profile_text).entries
        self.assertEqual(entry.ratio, 49)  # from the first CPU that needs a step (CPU 8: 48 + 1)
        # CPU 9 alone needing a step: the ramp comes from it
        readings[9] = measured_readings(9, VfWord(53, E_V, False, 0).to_u32())
        readings.update({c: measured_readings(c, VfWord(50, E_V, False, 0).to_u32()) for c in (8, 10, 11)})
        plan = build_plan(profile(e_core(0, ratio=50)), TOPO, readings, per_core_enabled=True, policy=policy_with(**{**DEFAULT_STEPS, **RAISE}))
        self.assertEqual(load_profile(plan.next_profile_text).entries[0].ratio, 52)  # 53 - 1

    def test_one_entry_per_cluster_in_the_next_profile(self) -> None:
        plan = e_plan(profile(e_core(0, ratio=50), e_core(4, ratio=49)), cpus=tuple(range(8, 16)))
        entries = load_profile(plan.next_profile_text).entries
        self.assertEqual([(e.kind, e.index, e.ratio) for e in entries], [("E", 0, 49), ("E", 4, 49)])


class TheHardwareMaximumRead(unittest.TestCase):
    def test_0x28_is_read_only_where_an_oc_point_voltage_is_set(self) -> None:
        with_v = resolve_targets(profile(e_core(0, oc_voltage_mv=1176), p4(ratio=56)), TOPO)
        cmds = [(s.cpu, s.cmd) for s in read_steps(with_v, POLICY)]
        self.assertEqual([c for c in cmds if c[1] == 0x28], [(8, 0x28), (9, 0x28), (10, 0x28), (11, 0x28)])  # the E cluster, not P-Core 4
        none = resolve_targets(profile(e_core(0, ratio=49)), TOPO)
        self.assertNotIn(0x28, [s.cmd for s in read_steps(none, POLICY)])

    def test_a_plan_without_the_0x28_word_refuses_a_raised_v_target(self) -> None:
        readings = {c: dataclasses.replace(measured_readings(c), vmax=None) for c in (8, 9, 10, 11)}
        plan = build_plan(profile(e_core(0, oc_voltage_mv=1176)), TOPO, readings, per_core_enabled=True, policy=POLICY)
        self.assertFalse(plan.allowed)
        self.assertTrue(any("0x28" in r.detail for r in plan.refusals))

    def test_the_plan_refuses_above_the_hardware_maximum(self) -> None:
        readings = {c: dataclasses.replace(measured_readings(c), vmax=0x4B0) for c in (8, 9, 10, 11)}  # hardware max 0x4AF = 1199 raw
        policy = policy_with(max_ratio_step=8, max_v_step_mv=200, max_offset_step_mv=200, **RAISE)
        plan = build_plan(profile(e_core(0, oc_voltage_mv=1200)), TOPO, readings, per_core_enabled=True, policy=policy)
        self.assertTrue(any("hardware maximum" in r.detail for r in plan.refusals))
        self.assertFalse(plan.needs_ramp)


class PreflightAgainstExplicitBounds(unittest.TestCase):
    def refused(self, prof, fragment: str):
        with self.assertRaises(PlanRefused) as ctx:
            preflight(prof, TOPO, POLICY)
        self.assertTrue(all(r.reason == "CHECK_FAILED" for r in ctx.exception.refusals))
        self.assertIn(fragment, ctx.exception.refusals[0].detail)
        return ctx.exception

    def test_values_outside_the_bounds_are_refused_before_any_read(self) -> None:
        self.refused(profile(e_core(0, ratio=51)), "ratio_max[E] = 50")
        self.refused(profile(p4(ratio=59)), "ratio_max[P] = 58")
        self.refused(profile(e_core(0, oc_voltage_mv=1251)), "v_target_max_mv[E] = 1250")
        self.refused(profile(p4(oc_voltage_mv=1501)), "v_target_max_mv[P] = 1500")
        self.refused(profile(e_core(0, offset_mv=26)), "offset_ceiling_mv 25")  # +26 mV = raw 27 > the ceiling raw 26
        self.refused(profile(p4(offset_mv=-51)), "D-015")

    def test_values_on_the_bounds_pass(self) -> None:
        for prof in (profile(e_core(0, ratio=50)), profile(p4(ratio=58)), profile(e_core(0, oc_voltage_mv=1250)), profile(e_core(0, offset_mv=25)), profile(p4(offset_mv=-50))):
            with self.subTest(prof=prof.entries[0]):
                preflight(prof, TOPO, policy_with(max_ops=400, **{**DEFAULT_STEPS, **RAISE}))

    def test_an_unset_ceiling_cannot_be_judged_before_the_reads(self) -> None:
        # no policy number for P V target here: the no-raise value depends on the words read, so pre-flight lets it through
        policy = policy_with(**{**DEFAULT_STEPS, "ratio_max": {"E": 50, "P": 58}})
        preflight(profile(p4(oc_voltage_mv=1600)), TOPO, policy)

    def test_the_offset_window_follows_the_policy(self) -> None:
        policy = policy_with(**{**DEFAULT_STEPS, "offset_ceiling_mv": 0})
        self.refused_with(policy, profile(e_core(0, offset_mv=1)), "offset_ceiling_mv 0")

    def refused_with(self, policy, prof, fragment: str) -> None:
        with self.assertRaises(PlanRefused) as ctx:
            preflight(prof, TOPO, policy)
        self.assertIn(fragment, ctx.exception.refusals[0].detail)

    def test_the_count_of_a_raise_profile_is_the_worst_case_with_every_extra(self) -> None:
        from arltuner.controls import NeedsHigherLimit

        prof = profile(e_core(0, ratio=49, oc_voltage_mv=1176))
        preflight(prof, TOPO, policy_with(max_ops=55, **{**DEFAULT_STEPS, **RAISE}))
        with self.assertRaises(NeedsHigherLimit) as ctx:
            preflight(prof, TOPO, policy_with(max_ops=54, **{**DEFAULT_STEPS, **RAISE}))
        self.assertEqual(ctx.exception.required, 55)  # 0x14 + 4 x (5 reads + 4 writes) + 0x15 = 38 operations + 17 read-backs


class TheCurrentStateIsKept(unittest.TestCase):
    def test_xtu_word_keeps_a_v_target_the_core_already_carries(self) -> None:
        # D-016: a V target set earlier (e.g. by an earlier ramp step) is NOT reset to the fused voltage
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


if __name__ == "__main__":
    unittest.main()
