"""T-014 / T-021 planner tests (pure: no port, no guard, no files).

Rules under test: docs/tasks/T-014-controls-cli.md "Controls - required behaviour" and docs/tasks/T-021-align-with-xtu-measured.md
(the step list and word XTU sends on a per-core Apply, ``[MEASURED win 2026-10-01]``, win-xtu-live-trace.md S-04...S-07); V/F word
rounding from registers.md ("V/F data word"). Every assertion is one a removed behaviour would break.
"""
from __future__ import annotations

import dataclasses
import unittest
from unittest import mock

from arltuner.controls import (
    REVERT_NOTE,
    XTU_SHAPE_CAVEAT,
    CoreReadings,
    NeedsBootBudget,
    NeedsHigherLimit,
    PlanError,
    PlanRefused,
    StepKind,
    build_plan,
    count_operations,
    fill_ratio,
    format_plan,
    preflight,
    resolve_targets,
    xtu_word,
)
from arltuner.guard import CHECKS, Policy, Request, default_m1_policy
from arltuner.guard.policy import build_context
from arltuner.mailbox import VfWord
from arltuner.msr import measured_baseline as measured
from arltuner.topology import arl_s_285k

from ._support import E_BASELINE, E_READINGS, P4_BASELINE, P4_READINGS, e_core, p4, policy_with, profile, readings_of

TOPO = arl_s_285k()
POLICY = policy_with()


def plan_for(prof, baseline=None, *, per_core_enabled=False, policy=POLICY, misc_word=None):
    """``baseline`` maps CPU -> ``CoreReadings`` (or a bare ``VfWord``: the readings the old SIM-ONLY defaults would give)."""
    base = {4: P4_READINGS} if baseline is None else {c: readings_of(v, top=57 if c < 8 else 46) if isinstance(v, VfWord) else v for c, v in baseline.items()}
    return build_plan(prof, TOPO, base, per_core_enabled=per_core_enabled, policy=policy, misc_word=misc_word)


def measured_readings(cpu: int) -> CoreReadings:
    """The measured machine's words for ``cpu`` (Linux numbering): 0x10 (0x0 except CPU 0), 0x1C, 0x09 P1=0x11, 0x26."""
    return CoreReadings(
        vf=VfWord.from_u32(measured.VF_DOMAIN0[cpu]),
        favored=measured.FAVORED_WORD[cpu],
        fused=measured.FUSED_POINT[cpu],
        granular=measured.GRANULAR_WORD[cpu],
    )


def p_core(index: int, offset_mv: int | None = None) -> str:
    return f'kind = "P"\nindex = {index}' + ("" if offset_mv is None else f"\noffset_mv = {offset_mv}")


def write_words(plan, cpu: int) -> tuple[int, int, int]:
    """(0x11 word, 0x1D data, 0x27 data) of ``cpu``."""
    by_cmd = {s.cmd: s.data for s in plan.steps if s.cpu == cpu and s.kind.is_write}
    return by_cmd[0x11], by_cmd.get(0x1D), by_cmd.get(0x27)  # G-122: 0x1D / 0x27 only when the profile sets an OC ratio (else None)


class KeptWord(unittest.TestCase):
    """G-124 [MEASURED lnx 2026-10-11/12]: a word without an OC point (ratio <= the top fused ratio) always keeps V target 0, whatever the offset."""

    def test_no_oc_point_drops_the_target_at_every_offset(self) -> None:
        from arltuner.controls.planner import kept_word

        for top, ratio in ((57, 57), (57, 55), (46, 46), (46, 40)):  # (top fused ratio of the core, OC ratio)
            for offset in (0, -10, 5):
                word = VfWord(ratio, 0x5B4, False, offset)
                self.assertEqual(kept_word(top, word), word.with_(v_target_raw=0), (top, ratio, offset))

    def test_an_oc_point_keeps_its_target(self) -> None:
        from arltuner.controls.planner import kept_word

        for top, ratio in ((57, 58), (46, 47), (46, 48)):
            word = VfWord(ratio, 0x48C, False, 0)
            self.assertEqual(kept_word(top, word), word, (top, ratio))
        word = VfWord(40, 0x48C, False, 0)
        self.assertEqual(kept_word(None, word), word)  # the top fused ratio is unknown (0x09 not read): nothing is dropped


class RaiseFromNoOcPoint(unittest.TestCase):
    """G-124 / G-241: at or below the top fused ratio the hardware keeps no V target, so there is no voltage-first word; ratio + voltage together create the OC point in
    ONE word (the ratio is usable only after the 0x1D the plan sends next). The two-word order stays for a core that already has an OC point."""

    RAISE = policy_with()  # D-049: the CPU's ratio limit (120x) is the only ceiling bound
    P57 = {4: readings_of(P4_BASELINE.with_(ratio=57))}
    CREATES = "creates the OC point (ratio + voltage in one word; the ratio is usable only after 0x1D)"

    @staticmethod
    def no_oc(vf: VfWord, top: int, fused_v: int, favored: int) -> CoreReadings:
        """A core with no OC point: ``vf`` has V target 0, the fused point (``top`` x at ``fused_v``) and 0x28 are read."""
        return dataclasses.replace(readings_of(vf, top=top, fused=top | fused_v << 8, favored=favored), vmax=0x800)

    def test_ratio_and_voltage_together_create_the_oc_point_in_one_word(self) -> None:
        plan = plan_for(profile(p4(ratio=58, oc_voltage_mv=1400)), self.P57, policy=self.RAISE)
        writes = [s for s in plan.steps if s.kind.is_write and s.kind is not StepKind.WRITE_PERCORE_ENABLE]
        self.assertEqual([s.kind for s in writes], [StepKind.WRITE_VF, StepKind.WRITE_FAVORED, StepKind.WRITE_GRANULAR])  # one 0x11, then 0x1D / 0x27
        vf = writes[0]
        self.assertEqual(vf.stage, self.CREATES)
        self.assertEqual((vf.after.ratio, vf.after.v_target_raw), (58, VfWord.v_to_raw(1.4)))
        self.assertIn("[creates the OC point", vf.purpose)
        self.assertEqual(writes[1].data, 58)

    def test_the_guard_accepts_the_creating_word_when_the_target_is_the_fused_voltage(self) -> None:
        """P0 58x on 2026-10-19: ``0x0005B43A`` = 58x at the fused top voltage 0x5B4 was accepted [MEASURED lnx 2026-10-19]."""
        r = self.no_oc(VfWord(57, 0, False, 0), 57, 0x5B4, 0x3939)
        plan = plan_for(profile(p4(ratio=58, oc_voltage_mv=1426)), {4: r}, policy=self.RAISE)
        vf = next(s for s in plan.steps if s.kind is StepKind.WRITE_VF)
        self.assertEqual(vf.data, 0x0005B43A)
        self.assertTrue(vf.verdict.allowed, vf.verdict.detail)
        self.assertTrue(plan.allowed, plan.refusals)

    def test_e_cluster_oc_point_is_one_word_per_cpu_then_1d_27_and_no_table_unless_drafted(self) -> None:
        r = self.no_oc(VfWord(46, 0, False, 0), 46, 0x4A0, 46 << 8 | 46)
        base = {c: r for c in range(8, 12)}
        plan = plan_for(profile(e_core(0, ratio=50, oc_voltage_mv=1176)), base, policy=self.RAISE)  # 0x4A0 / 1024 V = 1.1719 V; 1176 mV -> 0x4B2
        seq = [(s.cpu, s.kind) for s in plan.steps if s.kind.is_write and s.kind is not StepKind.WRITE_PERCORE_ENABLE]
        for cpu in range(8, 12):
            self.assertEqual([k for c, k in seq if c == cpu], [StepKind.WRITE_VF, StepKind.WRITE_FAVORED, StepKind.WRITE_GRANULAR], cpu)
        self.assertFalse(any(s.kind is StepKind.WRITE_MSR for s in plan.steps))  # no 0x650 write: the operator drafted no table
        self.assertEqual({s.stage for s in plan.steps if s.kind is StepKind.WRITE_VF}, {self.CREATES})

    def test_the_voltage_first_order_stays_when_an_oc_point_exists(self) -> None:
        oc = {4: readings_of(P4_BASELINE.with_(ratio=58), top=57)}
        plan = plan_for(profile(p4(ratio=59, oc_voltage_mv=1400)), oc, policy=self.RAISE)
        self.assertEqual([s.stage for s in plan.steps if s.kind is StepKind.WRITE_VF], ["1/2 voltage first", "2/2 full word"])

    def test_falling_ratio_and_voltage_stay_frequency_first(self) -> None:
        from arltuner.controls.planner import _vf_words

        words = _vf_words(VfWord(60, 0x600, False, 0), VfWord(58, 0x5B4, False, 0), 57, "x")
        self.assertEqual([stage for _, stage in words], ["1/2 frequency first", "2/2 full word"])

    def test_without_a_0x1d_after_it_the_old_refusal_stays(self) -> None:
        from arltuner.controls.planner import _vf_words

        with self.assertRaises(PlanError) as ctx:
            _vf_words(VfWord(57, 0, False, 0), VfWord(58, 0x5B4, False, 0), 57, "P-Core 4", sends_1d=False)
        self.assertIn("raise the ratio alone first", str(ctx.exception))

    def test_ratio_alone_then_voltage_alone_are_allowed(self) -> None:
        plan = plan_for(profile(p4(ratio=58)), self.P57, policy=self.RAISE)
        self.assertTrue(plan.allowed, plan.refusals)
        word = next(s for s in plan.steps if s.kind is StepKind.WRITE_VF).after
        self.assertEqual((word.ratio, word.v_target_raw), (58, 0x544))  # an OC point: the current target is kept
        up = {4: dataclasses.replace(readings_of(word), vmax=0x800)}  # 0x28 read: the hardware maximum bounds a raised V target
        plan = plan_for(profile(p4(oc_voltage_mv=1336)), up, policy=self.RAISE)
        self.assertTrue(plan.allowed, plan.refusals)



    def test_p_core_offset_sequence_follows_the_xtu_procedure(self) -> None:
        plan = plan_for(profile(p4(offset_mv=-30)))
        self.assertEqual(
            [(s.kind, s.cmd, s.cpu, s.p1, s.p2) for s in plan.steps],
            [
                (StepKind.READ_PERCORE_ENABLE, 0x14, 4, 0, 0),
                (StepKind.READ_VF, 0x10, 4, 0, 0),
                (StepKind.READ_FAVORED, 0x1C, 4, 0, 0),
                (StepKind.READ_FUSED, 0x09, 4, 0x11, 0),
                (StepKind.READ_GRANULAR, 0x26, 4, 0, 0),
                (StepKind.WRITE_PERCORE_ENABLE, 0x15, 4, 0, 0),
                (StepKind.WRITE_VF, 0x11, 4, 0, 0),
            ],
        )
        self.assertEqual([s.number for s in plan.steps], list(range(1, 8)))
        self.assertEqual((plan.guard_operations, plan.required_operations), (7, 9))  # 7 operations + 2 read-backs (0x15, 0x11); G-122: no 0x1D / 0x27 without an OC ratio
        self.assertTrue(plan.allowed)
        self.assertTrue(all(s.purpose.strip() for s in plan.steps))

    def test_reads_carry_no_payload(self) -> None:
        plan = plan_for(profile(p4(offset_mv=-30)))
        for s in plan.steps:
            if not s.kind.is_write:
                self.assertEqual(s.data, 0)

    def test_every_read_precedes_every_write(self) -> None:
        # pre-flight judges every write on the real words, so no write (the 0x15 included) may precede a read
        plan = plan_for(profile(p4(offset_mv=-30), e_core(0, offset_mv=-20)), {c: (P4_READINGS if c == 4 else E_READINGS) for c in (4, 8, 9, 10, 11)})
        kinds = [s.kind.is_write for s in plan.steps]
        self.assertEqual(kinds, sorted(kinds))

    def test_new_word_changes_only_the_requested_field(self) -> None:
        plan = plan_for(profile(p4(offset_mv=-30)))
        write = next(s for s in plan.steps if s.kind is StepKind.WRITE_VF)
        self.assertEqual(write.before, P4_BASELINE)
        kept = P4_BASELINE.with_(offset_raw=-31, v_target_raw=0)  # G-120: ratio 55 <= 57 = no OC point: the hardware keeps no V target next to an offset
        self.assertEqual(write.after, kept)  # (30*1024+512)//1000 = 31, sign kept
        self.assertEqual(write.data, kept.to_u32())
        self.assertEqual(write.request().data, write.data)

    def test_ratio_only_and_voltage_only_leave_the_rest_alone(self) -> None:
        def word(prof, baseline=None):
            return next(s for s in plan_for(prof, baseline).steps if s.kind is StepKind.WRITE_VF).after

        # G-124: P4 at 55x has no OC point, so any word on it carries V target 0 (the hardware keeps none)
        self.assertEqual(word(profile(p4(ratio=50))), P4_BASELINE.with_(ratio=50, v_target_raw=0))
        self.assertEqual(word(profile(p4(oc_voltage_mv=1300))), P4_BASELINE.with_(v_target_raw=0))
        oc = {4: readings_of(P4_BASELINE.with_(ratio=58))}  # an OC point (58 > 57) carries the target
        w = word(profile(p4(oc_voltage_mv=1300)), oc)
        self.assertEqual(w, P4_BASELINE.with_(ratio=58, v_target_raw=VfWord.v_to_raw(1.3)))  # 1331: integer mV snaps to 1/1024 V
        self.assertEqual(w.v_target_raw, 1331)
        # ratio 55 -> 50 and V target 1348 -> 1331 both fall: two 0x11 words (frequency first); the LAST one is the full word
        full = [s for s in plan_for(profile(p4(offset_mv=-30, ratio=50, oc_voltage_mv=1300))).steps if s.kind is StepKind.WRITE_VF][-1]
        self.assertEqual(full.after, VfWord(50, 0, False, -31))  # G-120: no OC point at ratio 50 -> the 1300 mV target is not written (warned)

    def test_static_bit_is_never_changed(self) -> None:
        static = VfWord(55, 0x544, True, 0)
        plan = plan_for(profile(p4(offset_mv=-30)), {4: static})
        w = next(s for s in plan.steps if s.kind is StepKind.WRITE_VF)
        self.assertTrue(w.after.static)
        self.assertEqual(w.after, static.with_(offset_raw=-31, v_target_raw=0))

    def test_per_core_state_decides_the_0x15_step(self) -> None:
        enabled = plan_for(profile(p4(offset_mv=-30)), per_core_enabled=True)
        self.assertEqual([s.cmd for s in enabled.steps], [0x14, 0x10, 0x1C, 0x09, 0x26, 0x11])
        self.assertEqual((enabled.guard_operations, enabled.read_back_requests, enabled.required_operations), (6, 1, 7))
        clear = plan_for(profile(p4(offset_mv=-30)), per_core_enabled=False)
        self.assertEqual([s.cmd for s in clear.steps], [0x14, 0x10, 0x1C, 0x09, 0x26, 0x15, 0x11])
        unknown = plan_for(profile(p4(offset_mv=-30)), per_core_enabled=None)
        step = unknown.steps[5]
        self.assertEqual(step.cmd, 0x15)
        self.assertTrue(step.conditional)
        self.assertIsNone(step.data)  # old|0x8 is not known yet
        self.assertEqual((unknown.guard_operations, unknown.required_operations), (7, 9))  # worst case: + one read-back per write
        with self.assertRaises(ValueError):
            step.request()

    def test_0x15_data_is_old_or_8_when_the_word_is_known(self) -> None:
        plan = plan_for(profile(p4(offset_mv=-30)), per_core_enabled=False, misc_word=0x1)
        self.assertEqual(plan.steps[5].data, 0x9)
        self.assertEqual(plan.steps[5].request().data, 0x9)
        self.assertFalse(plan.steps[5].conditional)
        derived = plan_for(profile(p4(offset_mv=-30)), per_core_enabled=None, misc_word=0x8)
        self.assertTrue(derived.per_core_enabled)
        self.assertEqual([s.cmd for s in derived.steps], [0x14, 0x10, 0x1C, 0x09, 0x26, 0x11])
        with self.assertRaises(ValueError):
            plan_for(profile(p4(offset_mv=-30)), per_core_enabled=False, misc_word=0x8)
        with self.assertRaises(ValueError):
            plan_for(profile(p4(offset_mv=-30)), per_core_enabled=True, misc_word=0x0)
        with self.assertRaises(TypeError):
            plan_for(profile(p4(offset_mv=-30)), per_core_enabled=1)  # type: ignore[arg-type]

    def test_0x1d_follows_the_ratio_of_the_0x11_word_and_0x27_the_0x26_word(self) -> None:
        plan = plan_for(profile(p4(offset_mv=-30, ratio=50)), {4: readings_of(P4_BASELINE, granular=0x205)})
        w11, w1d, w27 = write_words(plan, 4)
        self.assertEqual(VfWord.from_u32(w11).ratio, 50)
        self.assertEqual(w1d, 50)  # = the ratio of the word written, not the baseline's 55
        self.assertEqual(w27, 0x205)  # = the 0x26 word read, exactly


class MeasuredShapes(unittest.TestCase):
    """The sequences XTU sent on this machine (S-04, S-04c, S-04d, S-06/S-07), reproduced from the measured baseline words."""

    def plan(self, prof, cpus, per_core_enabled=True):
        return build_plan(prof, TOPO, {c: measured_readings(c) for c in cpus}, per_core_enabled=per_core_enabled, policy=policy_with(max_ops=200))

    def test_cpu0_minus_5_mv_is_xtus_word(self) -> None:
        plan = self.plan(profile(p_core(0, -5)), [0])
        self.assertEqual(write_words(plan, 0), (0xFF600039, None, None))  # G-120: P0's OC ratio 57 = top fused ratio, no OC point: ratio | offset << 21, target 0 [MEASURED lnx 2026-10-11]
        self.assertTrue(plan.allowed, plan.refusals)

    def test_cpu0_minus_15_mv_is_xtus_word(self) -> None:
        self.assertEqual(write_words(self.plan(profile(p_core(0, -15)), [0]), 0), (0xFE200039, None, None))

    def test_cpu0_revert_word_keeps_the_fused_v_target_not_the_zero_one(self) -> None:
        plan = self.plan(profile(p_core(0, 0)), [0])
        self.assertEqual(write_words(plan, 0), (0x00000039, None, None))  # G-124: XTU's revert word minus the V target the hardware cannot keep (= the cold-boot word)
        self.assertTrue(plan.allowed, plan.refusals)

    def test_cpu1_ratio_comes_from_0x1c_and_v_target_from_0x09(self) -> None:
        plan = self.plan(profile(p_core(1, -5)), [1])
        self.assertEqual(write_words(plan, 1), (0xFF600037, None, None))  # ratio 0x37 = 55 (0x1C), not 57 (0x09); no OC point -> V target 0
        self.assertEqual(write_words(self.plan(profile(p_core(1, 0)), [1]), 1), (0x00000037, None, None))  # G-124: no OC point, target 0
        self.assertTrue(plan.allowed, plan.refusals)

    def test_cpu1_step_list_equals_xtus_with_and_without_the_0x15(self) -> None:
        enabled = self.plan(profile(p_core(1, -5)), [1], per_core_enabled=True)
        self.assertEqual([s.cmd for s in enabled.steps], [0x14, 0x10, 0x1C, 0x09, 0x26, 0x11])
        self.assertEqual([(s.cmd, s.p1) for s in enabled.steps if s.cmd == 0x09], [(0x09, 0x11)])
        clear = self.plan(profile(p_core(1, -5)), [1], per_core_enabled=False)
        self.assertEqual([s.cmd for s in clear.steps], [0x14, 0x10, 0x1C, 0x09, 0x26, 0x15, 0x11])

    def test_no_word_with_a_zero_ratio_is_ever_planned_and_a_zero_v_target_only_without_an_oc_point(self) -> None:
        for cpu in range(24):
            prof = profile(p_core(cpu, -5)) if cpu < 8 else profile(e_core(cpu - 8, -5))
            cpus = [cpu] if cpu < 8 else list(range(8 + 4 * ((cpu - 8) // 4), 12 + 4 * ((cpu - 8) // 4)))
            plan = self.plan(prof, cpus)
            for c in cpus:
                w = VfWord.from_u32(write_words(plan, c)[0])
                self.assertNotEqual((w.ratio, w.v_target_raw == 0), (0, True), c)
                self.assertGreater(w.ratio, 0, c)
                # G-120/G-122: P ratio <= 57, E ratio capped at 46 (not 0x1C[7:0] = 48): no OC point -> the kept word has no target
                self.assertEqual(w.v_target_raw, 0, c)
                self.assertLessEqual(w.ratio, 57 if cpu < 8 else 46, c)
                self.assertFalse(w.static)
            self.assertTrue(plan.allowed, (cpu, plan.refusals))

    def test_e_cluster_words_equal_xtus_for_all_16_cores(self) -> None:
        prof = profile(e_core(0, offset_mv=-5), e_core(4, offset_mv=-5), e_core(8, offset_mv=-5), e_core(12, offset_mv=-5))
        plan = self.plan(prof, range(8, 24))
        for cpu in range(8, 24):  # G-122: ratio 46 = the top fused E ratio (not 0x1C[7:0] = 48: an OC point), V target 0, offset -5 << 21; 0x1D carries the ratio
            self.assertEqual(write_words(plan, cpu), (0xFF60002E, None, None), cpu)
        self.assertTrue(plan.allowed, plan.refusals)
        self.assertEqual((plan.guard_operations, plan.required_operations), (1 + 5 * 16, 1 + 5 * 16 + 16))
        reverts = profile(e_core(0, offset_mv=0), e_core(4, offset_mv=0), e_core(8, offset_mv=0), e_core(12, offset_mv=0))
        rplan = self.plan(reverts, range(8, 24))
        self.assertEqual({write_words(rplan, c)[0] for c in range(8, 12)}, {0x0000002E})  # G-124: offset 0 keeps no target either (kept_word)
        self.assertEqual({write_words(rplan, c)[0] for c in range(12, 24)}, {0x0000002E})

    def test_a_baseline_word_is_used_when_non_zero_and_a_missing_reference_is_refused_not_invented(self) -> None:
        # a core whose 0x10 word carries ratio 50: XTU keeps it (ratio = the non-zero baseline ratio, not 0x1C[7:0])
        r = dataclasses.replace(measured_readings(1), vf=VfWord(50, 0, False, 0))
        plan = build_plan(profile(p_core(1, -5)), TOPO, {1: r}, per_core_enabled=True, policy=POLICY)
        self.assertEqual(VfWord.from_u32(write_words(plan, 1)[0]).ratio, 50)
        # no 0x1C ratio and no baseline ratio: the word would carry ratio 0 -> predicted refusal, nothing made up
        none = dataclasses.replace(measured_readings(1), favored=0)
        refused = build_plan(profile(p_core(1, -5)), TOPO, {1: none}, per_core_enabled=True, policy=POLICY)
        self.assertFalse(refused.allowed)
        self.assertTrue(any("ratio 0" in r.detail for r in refused.refusals), refused.refusals)
        # G-122: an offset-only E word has no OC point and needs no voltage reference; an OC ratio above 46 does (its point carries a target)
        refused = build_plan(profile(e_core(0, ratio=47)), TOPO, {c: dataclasses.replace(measured_readings(c), fused=0x2E) for c in range(8, 12)}, per_core_enabled=True, policy=POLICY)
        self.assertTrue(any("v_target_raw 0" in r.detail for r in refused.refusals), refused.refusals)

    def test_the_e_offset_only_word_has_no_oc_point_and_a_ratio_raise_is_judged_by_the_cpu_limit(self) -> None:
        """G-122: the E offset-only word has ratio 46 (no OC point); the guard measures a cold word at or below the top fused ratio from min(0x1C[7:0], top fused).
        A user-set OC ratio keeps the XTU fill. D-049: there is no step limit; a raise is judged by the CPU's own ratio limit."""
        readings = {c: measured_readings(c) for c in range(12, 16)}
        small = policy_with(max_ops=200)
        plan = build_plan(profile(e_core(4, offset_mv=-10)), TOPO, readings, per_core_enabled=True, policy=small)
        self.assertEqual({write_words(plan, c) for c in range(12, 16)}, {(0xFEC0002E, None, None)})  # ratio 46, target 0, -10 mV; no 0x1D / 0x27
        self.assertTrue(plan.allowed, plan.refusals)
        self.assertEqual([s.cmd for s in plan.steps if s.cpu == 12 and s.kind.is_write], [0x11])
        # an OC ratio / OC voltage the profile sets itself is the user's: the XTU fill (48, an OC point)
        for entry in (e_core(4, ratio=48), e_core(4, oc_voltage_mv=1137)):
            own = build_plan(profile(entry), TOPO, readings, per_core_enabled=True, policy=small)
            self.assertEqual({VfWord.from_u32(write_words(own, c)[0]).ratio for c in range(12, 16)}, {48}, entry)
        raise_p = build_plan(profile(p_core(1, -5)), TOPO, {1: measured_readings(1)}, per_core_enabled=True, policy=small)
        self.assertEqual(VfWord.from_u32(write_words(raise_p, 1)[0]).ratio, 55)  # a P core is unaffected
        # a ratio raise is allowed up to the CPU's limit and refused above it: P1 cold (0x1C[7:0] = 55) asking for 57 is two bins
        ask = profile('kind = "P"\nindex = 1\nratio = 57')
        two = build_plan(ask, TOPO, {1: measured_readings(1)}, per_core_enabled=True, policy=dataclasses.replace(small, hw_ratio_max={"P": 57, "E": 120}))
        self.assertTrue(two.allowed, two.refusals)
        low = build_plan(ask, TOPO, {1: measured_readings(1)}, per_core_enabled=True, policy=dataclasses.replace(small, hw_ratio_max={"P": 56, "E": 120}))
        self.assertFalse(low.allowed)
        self.assertTrue(any("cmd 0x01" in r.detail for r in low.refusals), low.refusals)

    def test_fill_ratio_caps_a_zero_baseline_at_the_top_fused_ratio_only(self) -> None:
        self.assertEqual(fill_ratio(0, 0x2E30, 46), 46)
        self.assertEqual(fill_ratio(0, 0x3737, 57), 55)
        self.assertEqual(fill_ratio(0, 0x3939, 57), 57)
        self.assertEqual(fill_ratio(48, 0x2E30, 46), 48)  # a real baseline ratio (an existing OC point) is left alone
        self.assertEqual(fill_ratio(0, 0x2E30), 48)  # no top: XTU's fill
        self.assertEqual(fill_ratio(0, 0, 46), 0)

    def test_xtu_word_helper(self) -> None:
        self.assertEqual(xtu_word(VfWord.from_u32(0x39), 0x3939, 0x5B439).to_u32(), 0x0005B439)
        self.assertEqual(xtu_word(VfWord.from_u32(0x0), 0x3737, 0x53839).to_u32(), 0x00053837)
        self.assertEqual(xtu_word(VfWord.from_u32(0x0), 0x2E30, 0x4A02E).to_u32(), 0x0004A030)

    def test_effective_offsets_are_shown_in_the_plan_text(self) -> None:
        text = format_plan(self.plan(profile(p_core(0, -5), e_core(0, offset_mv=-5)), [0, 8, 9, 10, 11]))
        # D-049: no BIOS offset is assumed for either kind and the plan no longer explains a policy floor
        self.assertIn("runtime offset -4.88 mV, effective -4.88 mV (assumed BIOS offset +0 mV)", text)  # P core
        self.assertIn("runtime offset -4.88 mV, effective -4.88 mV (assumed BIOS offset +0 mV)", text)  # E core
        self.assertNotIn("effective offset = assumed BIOS offset", text)


class Targets(unittest.TestCase):
    def test_e_entry_applies_to_the_whole_cluster_once(self) -> None:
        targets = resolve_targets(profile(e_core(12, offset_mv=-20)), TOPO)
        self.assertEqual([t.cpu for t in targets], [20, 21, 22, 23])  # XTU E-Core 12 = CPU 20, cluster 3
        self.assertEqual([t.label for t in targets], ["E-Core 12", "E-Core 13", "E-Core 14", "E-Core 15"])
        base = {c: E_READINGS for c in (20, 21, 22, 23)}
        plan = plan_for(profile(e_core(12, offset_mv=-20)), base)
        writes = [s for s in plan.steps if s.kind is StepKind.WRITE_VF]
        self.assertEqual([s.cpu for s in writes], [20, 21, 22, 23])
        self.assertEqual({s.data for s in writes}, {E_BASELINE.with_(offset_raw=-20).to_u32()})
        self.assertEqual((plan.guard_operations, plan.required_operations), (1 + 4 * 4 + 1 + 4, 1 + 4 * 4 + 1 + 4 + 1 + 4))
        self.assertEqual([s for s in plan.steps if s.kind is StepKind.WRITE_FAVORED], [])  # G-122: offset-only, no 0x1D

    def test_entries_of_one_cluster_are_deduplicated(self) -> None:
        both = profile(e_core(12, offset_mv=-20), e_core(13, offset_mv=-20))
        self.assertEqual([t.cpu for t in resolve_targets(both, TOPO)], [20, 21, 22, 23])

    def test_mixed_p_and_e_profile(self) -> None:
        prof = profile(p4(offset_mv=-30), e_core(0, offset_mv=-20))
        targets = resolve_targets(prof, TOPO)
        self.assertEqual([t.cpu for t in targets], [4, 8, 9, 10, 11])
        base = {4: P4_READINGS, **{c: E_READINGS for c in (8, 9, 10, 11)}}
        plan = plan_for(prof, base)
        self.assertEqual((plan.guard_operations, plan.required_operations), (1 + 5 * 4 + 1 + 5, 1 + 5 * 4 + 1 + 5 + 1 + 5))
        self.assertEqual([s.cmd for s in plan.steps], [0x14] + [0x10, 0x1C, 0x09, 0x26] * 5 + [0x15] + [0x11] * 5)
        self.assertEqual(plan.steps[0].cpu, 4)  # 0x14 / 0x15 go to the first target CPU
        self.assertEqual([s.core_kind for s in plan.steps if s.kind is StepKind.WRITE_VF], ["P", "E", "E", "E", "E"])

    def test_missing_or_bad_baseline_is_an_error(self) -> None:
        with self.assertRaises(PlanError):
            plan_for(profile(p4(offset_mv=-30)), {})
        with self.assertRaises(PlanError):
            build_plan(profile(p4(offset_mv=-30)), TOPO, {4: P4_BASELINE}, per_core_enabled=False, policy=POLICY)  # type: ignore[dict-item]
        with self.assertRaises(ValueError):
            CoreReadings(vf=P4_BASELINE, favored=-1, fused=0, granular=0)
        with self.assertRaises(TypeError):
            CoreReadings(vf=0x544, favored=0, fused=0, granular=0)  # type: ignore[arg-type]

    def test_planning_is_pure(self) -> None:
        with mock.patch("os.open", side_effect=AssertionError("planner must not do I/O")), mock.patch(
            "builtins.open", side_effect=AssertionError("planner must not do I/O")
        ):
            plan = plan_for(profile(p4(offset_mv=-30)))
        self.assertEqual(plan.required_operations, 9)


class Counting(unittest.TestCase):
    def test_count_operations(self) -> None:
        # every WRMSR: the 0x14 read + per CPU (4 reads + 1 write: the 0x11; G-122: 0x1D / 0x27 only with an OC ratio) + the 0x15 write unless known set + ONE READ-BACK PER WRITE (T-022)
        self.assertEqual(count_operations(1, True), 1 + 4 + 1 + 1)
        self.assertEqual(count_operations(1, False), 1 + 4 + 2 + 2)
        self.assertEqual(count_operations(1, None), 9)  # unknown = worst case (the 0x15 write and its read-back included)
        self.assertEqual(count_operations(4, False), 17 + 5 + 5)
        self.assertEqual(count_operations(5, True), 21 + 5 + 5)
        self.assertEqual(count_operations(16, True), 65 + 16 + 16)
        with self.assertRaises(ValueError):
            count_operations(0, None)
        for bad in (True, [], (), None, 1.5, ["x"]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                count_operations(bad, None)  # type: ignore[arg-type]

    def test_the_extra_read_and_the_extra_write_of_a_raise_are_counted(self) -> None:
        offset_only, ratio_only, volts_only, both = (
            resolve_targets(profile(p4(**kw)), TOPO) for kw in ({"offset_mv": -5}, {"ratio": 56}, {"oc_voltage_mv": 1300}, {"ratio": 56, "oc_voltage_mv": 1300})
        )
        base = count_operations(offset_only, True)
        self.assertEqual(base, count_operations(1, True))
        self.assertEqual(count_operations(ratio_only, True), base + 2 + 2)  # a ratio alone: one 0x11, no 0x28, but the 0x1D and 0x27 (and their read-backs)
        self.assertEqual(count_operations(volts_only, True), base + 1)  # + the 0x28 read, still one 0x11
        self.assertEqual(count_operations(both, True), base + 1 + 6)  # + the 0x28 read + the 0x1D, 0x27 and a second 0x11, each with its read-back
        self.assertEqual(count_operations(both, None), base + 1 + 6 + 2)  # unknown 0x15 state: + the write and its read-back

    def test_the_guard_operations_and_the_budget_differ_by_the_read_backs_only(self) -> None:
        plan = plan_for(profile(p4(offset_mv=-30)), per_core_enabled=False)
        self.assertEqual(plan.guard_operations, len(plan.steps))
        self.assertEqual(plan.read_back_requests, sum(1 for s in plan.steps if s.kind.is_write))
        self.assertEqual(plan.required_operations, plan.guard_operations + plan.read_back_requests)
        self.assertEqual(plan.estimated_min_seconds, 60.0 * plan.guard_operations)  # a read-back follows its write without a dwell

    def test_the_count_matches_the_plan_for_any_size_and_state(self) -> None:
        for n, state in ((1, True), (1, False), (4, False)):
            base = {c: E_READINGS for c in range(8, 8 + n)}
            prof = profile(e_core(0, offset_mv=-20)) if n == 4 else profile(p4(offset_mv=-30))
            if n == 1:
                base = {4: P4_READINGS}
            plan = plan_for(prof, base, per_core_enabled=state)
            self.assertEqual(plan.required_operations, count_operations(n, state))

    def test_needs_higher_limit_with_the_default_policy(self) -> None:
        plan = plan_for(profile(p4(offset_mv=-30)), policy=default_m1_policy())
        self.assertTrue(plan.needs_higher_limit)
        self.assertFalse(plan.allowed)
        self.assertEqual((plan.required_operations, plan.allowed_operations), (9, 1))
        with self.assertRaises(NeedsHigherLimit) as ctx:
            plan.require_allowed()
        self.assertEqual((ctx.exception.required, ctx.exception.allowed), (9, 1))
        with self.assertRaises(NeedsHigherLimit):
            preflight(profile(p4(offset_mv=-30)), TOPO, default_m1_policy())

    def test_exact_limit_is_enough(self) -> None:
        # 7 guard operations + 2 read-backs = 9 WRMSRs (G-122: no 0x1D / 0x27 for an offset-only profile): the budget counts the read-backs although the guard's own counter does not
        self.assertTrue(plan_for(profile(p4(offset_mv=-30)), policy=policy_with(max_ops=9)).allowed)
        self.assertFalse(plan_for(profile(p4(offset_mv=-30)), policy=policy_with(max_ops=8)).allowed)
        self.assertFalse(plan_for(profile(p4(offset_mv=-30)), policy=policy_with(max_ops=7)).allowed)  # enough for the guard, not for the budget
        preflight(profile(p4(offset_mv=-30)), TOPO, policy_with(max_ops=9))
        with self.assertRaises(NeedsHigherLimit) as ctx:
            preflight(profile(p4(offset_mv=-30)), TOPO, policy_with(max_ops=8))
        self.assertEqual((ctx.exception.required, ctx.exception.allowed), (9, 8))

    def test_the_default_per_boot_cap_cannot_cover_an_e_cluster(self) -> None:
        pol = dataclasses.replace(default_m1_policy(), max_operations_per_session=60)  # per-boot cap stays 12
        with self.assertRaises(NeedsBootBudget) as ctx:
            preflight(profile(e_core(12, offset_mv=-20)), TOPO, pol)
        self.assertEqual((ctx.exception.required, ctx.exception.remaining), (27, 12))  # 22 operations + 5 read-backs

    def test_preflight_counts_the_per_boot_budget_before_anything_is_sent(self) -> None:
        pol = policy_with(max_ops=60, boot_ops=16)  # a plan for one P-core needs 9 WRMSRs (7 operations + 2 read-backs)
        preflight(profile(p4(offset_mv=-30)), TOPO, pol, boot_ops_used=7)  # exactly enough: 16 - 7 == 9
        with self.assertRaises(NeedsBootBudget) as ctx:
            preflight(profile(p4(offset_mv=-30)), TOPO, pol, boot_ops_used=8)  # one short
        self.assertEqual((ctx.exception.required, ctx.exception.remaining, ctx.exception.used, ctx.exception.cap), (9, 8, 8, 16))
        with self.assertRaises(NeedsBootBudget) as ctx:  # an already exhausted budget never goes negative
            preflight(profile(p4(offset_mv=-30)), TOPO, pol, boot_ops_used=99)
        self.assertEqual(ctx.exception.remaining, 0)
        preflight(profile(p4(offset_mv=-30)), TOPO, pol)  # default: nothing used yet
        for bad in (-1, 1.5, True, "3"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                preflight(profile(p4(offset_mv=-30)), TOPO, pol, boot_ops_used=bad)  # type: ignore[arg-type]

    def test_estimated_min_seconds_is_dwell_times_operations(self) -> None:
        self.assertEqual(plan_for(profile(p4(offset_mv=-30)), policy=policy_with(dwell=60.0)).estimated_min_seconds, 420.0)
        self.assertEqual(plan_for(profile(p4(offset_mv=-30)), policy=policy_with(dwell=0.0)).estimated_min_seconds, 0.0)
        self.assertEqual(plan_for(profile(p4(offset_mv=-30)), per_core_enabled=True, policy=policy_with(dwell=10.0)).estimated_min_seconds, 60.0)


class PredictedVerdicts(unittest.TestCase):
    """Each case is a write the real guard refuses with CHECK_FAILED (tests/controls/test_executor.py proves the match).

    The 0x11 write of a P-core plan with the 0x15 step is step 7 (4 + 1 reads, the 0x15 at 6)."""

    def refused(self, prof, baseline=None, policy=POLICY, step=7):
        plan = plan_for(prof, baseline, policy=policy)
        self.assertFalse(plan.allowed)
        self.assertFalse(plan.needs_higher_limit)
        refusals = plan.refusals
        self.assertEqual(len(refusals), 1, refusals)
        self.assertEqual(refusals[0].reason, "CHECK_FAILED")
        self.assertEqual((refusals[0].cmd, refusals[0].step_number), (0x11, step))
        with self.assertRaises(PlanRefused) as ctx:
            plan.require_allowed()
        self.assertIs(ctx.exception.plan, plan)
        return refusals[0]

    def test_overvolt(self) -> None:
        # step 8: an OC-point voltage adds the 0x28 read before the writes; this reading has none (vmax None) = the CPU's voltage limit is unread and a raise is refused naming it
        detail = self.refused(profile(p4(oc_voltage_mv=1400)), {4: readings_of(P4_BASELINE.with_(ratio=58))}, step=8).detail  # G-124: P4 needs an OC point (58 > 57) to carry a V target
        self.assertIn("predicted voltage", detail)
        self.assertIn("CPU_LIMIT_UNREAD", detail)
        self.assertIn("cmd 0x28", detail)

    def test_overvolt_above_the_cpu_limit(self) -> None:
        read = dataclasses.replace(readings_of(P4_BASELINE.with_(ratio=58)), vmax=0x681)  # 0x28 = 1625 mV
        self.assertTrue(plan_for(profile(p4(oc_voltage_mv=1600)), {4: read}).allowed)
        detail = self.refused(profile(p4(oc_voltage_mv=1650)), {4: read}, step=8).detail
        self.assertIn("above the CPU limit", detail)
        self.assertIn("cmd 0x28", detail)

    def test_ratio_above_the_cpu_limit(self) -> None:
        self.assertTrue(plan_for(profile(p4(ratio=57))).allowed)  # D-049: a raise within the CPU's 120x is allowed
        self.assertIn("cmd 0x01", self.refused(profile(p4(ratio=121))).detail)

    def test_ratio_raise_with_the_cpu_limit_unread(self) -> None:
        detail = self.refused(profile(p4(ratio=57)), policy=dataclasses.replace(default_m1_policy(), max_operations_per_session=400, max_operations_per_boot=1000), step=8).detail  # the shipped policy has no limit bound: the plan reads 0x01 itself, the verdict needs its word
        self.assertIn("CPU_LIMIT_UNREAD", detail)

    def test_the_old_offset_window_is_gone(self) -> None:
        for mv in (-50, -51, -100, -150, -500, -1000):
            self.assertTrue(plan_for(profile(p4(offset_mv=mv))).allowed, mv)
        base = {c: E_READINGS for c in (20, 21, 22, 23)}
        for mv in (-100, -101, -300):
            self.assertTrue(plan_for(profile(e_core(12, offset_mv=mv)), base).allowed, mv)

    def test_positive_offset_needs_the_cpu_voltage_limit(self) -> None:
        detail = self.refused(profile(p4(offset_mv=10)), step=8).detail  # step 8: the positive offset adds the 0x28 read
        self.assertIn("CPU_LIMIT_UNREAD", detail)
        read = dataclasses.replace(P4_READINGS, vmax=0x681)
        self.assertTrue(plan_for(profile(p4(offset_mv=10)), {4: read}).allowed)

    def test_static_baseline_cannot_be_evaluated(self) -> None:
        detail = self.refused(profile(p4(offset_mv=-30)), {4: P4_BASELINE.with_(static=True)}).detail
        self.assertIn("static", detail)

    def test_at_or_below_baseline_is_allowed(self) -> None:
        self.assertTrue(plan_for(profile(p4(ratio=55, oc_voltage_mv=1300, offset_mv=-30))).allowed)
        self.assertTrue(plan_for(profile(p4(ratio=50))).allowed)  # lowering by 5 bins: inside the relaxed step limit of this fixture

    def test_prediction_uses_the_guard_predicate_and_the_same_session_facts(self) -> None:
        ctx = build_context(POLICY, 4, favored_word=P4_READINGS.favored, fused_word=P4_READINGS.fused, vf_word_sent=None)
        for mv in (-150, -101, -52, -51, -50, -30, 0, 10, 26, 150):
            with self.subTest(offset_mv=mv):
                plan = plan_for(profile(p4(offset_mv=mv)))
                write = next(s for s in plan.steps if s.kind is StepKind.WRITE_VF)
                ok, _ = CHECKS["vf_bounded"](write.request(), P4_BASELINE.to_u32(), {}, ctx)
                self.assertEqual(write.verdict.allowed, ok)

    def test_0x1d_and_0x27_are_predicted_with_the_guard_predicates(self) -> None:
        plan = plan_for(profile(p4(offset_mv=-30)))
        for step in plan.steps:
            if step.kind in (StepKind.WRITE_FAVORED, StepKind.WRITE_GRANULAR):
                self.assertTrue(step.verdict.allowed, step.verdict)
        # a 0x1D that does not follow the planned 0x11 word is what the guard would refuse
        w11 = next(s for s in plan.steps if s.kind is StepKind.WRITE_VF)
        ctx = build_context(POLICY, 4, favored_word=0, fused_word=0, vf_word_sent=w11.data)
        ok, _ = CHECKS["favored_ratio_follows_vf"](Request(4, 0x1D, 0, 0, 54, "x"), P4_READINGS.favored, {}, ctx)
        self.assertFalse(ok)

    def test_cpu_outside_the_policy_set(self) -> None:
        policy = dataclasses.replace(POLICY, cpus=frozenset({0, 1, 2, 3}))
        plan = plan_for(profile(p4(offset_mv=-30)), policy=policy)
        self.assertEqual({r.reason for r in plan.refusals}, {"CPU_NOT_ALLOWED"})
        self.assertEqual(len(plan.refusals), 7)  # every step is on cpu 4
        with self.assertRaises(PlanRefused):
            preflight(profile(p4(offset_mv=-30)), TOPO, policy)  # refused before any read

    def test_request_outside_the_allowlist(self) -> None:
        def without(cmd):
            return dataclasses.replace(POLICY, min_dwell_s=0.0, allow=tuple(e for e in POLICY.allow if e.cmd != cmd))

        no_vf_write = without(0x11)
        plan = plan_for(profile(p4(offset_mv=-30)), policy=no_vf_write)
        self.assertEqual([(r.step_number, r.reason) for r in plan.refusals], [(7, "NOT_ALLOWLISTED")])
        with self.assertRaises(PlanRefused):
            preflight(profile(p4(offset_mv=-30)), TOPO, no_vf_write)
        for cmd, step_number in ((0x14, 1), (0x1C, 3), (0x09, 4), (0x26, 5)):  # a read the policy does not list
            with self.subTest(read=hex(cmd)):
                policy = without(cmd)
                self.assertEqual([(r.step_number, r.reason) for r in plan_for(profile(p4(offset_mv=-30)), policy=policy).refusals], [(step_number, "NOT_ALLOWLISTED")])
                with self.assertRaises(PlanRefused) as ctx:
                    preflight(profile(p4(offset_mv=-30)), TOPO, policy)
                self.assertEqual(ctx.exception.refusals[0].cmd, cmd)
        for cmd, step_number in ((0x1D, 8), (0x27, 9)):  # a write the policy does not list: caught by pre-flight too (G-122: 0x1D / 0x27 exist only with an OC ratio)
            with self.subTest(write=hex(cmd)):
                policy = without(cmd)
                self.assertEqual([r.step_number for r in plan_for(profile(p4(offset_mv=-30, ratio=55)), policy=policy).refusals], [step_number])
                with self.assertRaises(PlanRefused) as ctx:
                    preflight(profile(p4(offset_mv=-30, ratio=55)), TOPO, policy)
                self.assertEqual([r.cmd for r in ctx.exception.refusals], [cmd])

    def test_0x15_is_predicted_ok_by_construction(self) -> None:
        plan = plan_for(profile(p4(offset_mv=-30)), per_core_enabled=False, misc_word=0x1)
        self.assertTrue(plan.steps[5].verdict.allowed)


class Warnings(unittest.TestCase):
    def test_the_xtu_shape_caveat_is_on_every_plan_and_says_what_is_unverified(self) -> None:
        for prof in (profile(p4(ratio=50)), profile(p4(offset_mv=-30))):
            self.assertIn(XTU_SHAPE_CAVEAT, plan_for(prof).warnings)
        self.assertIn("UNVERIFIED", XTU_SHAPE_CAVEAT)
        self.assertIn("0x1D/0x27", XTU_SHAPE_CAVEAT)
        self.assertIn("A V target of 0 is sent only on a word with no OC point", XTU_SHAPE_CAVEAT)  # G-122: D-035, the zero target is sent there
        self.assertIn("no OC point", XTU_SHAPE_CAVEAT)
        self.assertIn("A zero ratio is never sent", XTU_SHAPE_CAVEAT)
        self.assertNotIn("this plan never does", XTU_SHAPE_CAVEAT)
        self.assertNotIn("[MEASURED", XTU_SHAPE_CAVEAT)  # D-036: no dated evidence tags in user-facing text
        self.assertTrue(XTU_SHAPE_CAVEAT.isascii())
        self.assertNotIn("not modelled", XTU_SHAPE_CAVEAT)  # the T-014 ratio caveat is gone

    def test_cluster_wide_warning_for_e_cores(self) -> None:
        base = {c: E_READINGS for c in (20, 21, 22, 23)}
        warnings = plan_for(profile(e_core(12, offset_mv=-20)), base).warnings
        self.assertTrue(any("cluster" in w and "UNVERIFIED" in w for w in warnings))
        self.assertFalse(any("cluster" in w for w in plan_for(profile(p4(offset_mv=-30))).warnings))

    def test_no_bios_offset_note_any_more(self) -> None:
        for prof in (profile(p4(offset_mv=-30)), profile(p4(ratio=50))):
            self.assertFalse(any("assumed BIOS offset" in w or "floors the" in w for w in plan_for(prof).warnings))

    def test_d049_warnings_for_large_positive_and_deep_changes(self) -> None:
        def codes(prof, **kw):
            return [w.split(":", 1)[0] for w in plan_for(prof, **kw).warnings if w.split(":", 1)[0].isupper() and "_" in w.split(":", 1)[0]]

        read = dataclasses.replace(P4_READINGS, vmax=0x681)
        self.assertEqual(codes(profile(p4(offset_mv=-30))), [])  # a small undervolt warns about nothing
        self.assertIn("LARGE_CHANGE", codes(profile(p4(offset_mv=-60))))  # |delta| > 50 mV
        self.assertNotIn("DEEP_WHOLE_CURVE", codes(profile(p4(offset_mv=-60))))
        self.assertIn("DEEP_WHOLE_CURVE", codes(profile(p4(offset_mv=-150))))  # deeper than -100 mV
        self.assertIn("POSITIVE_OFFSET", codes(profile(p4(offset_mv=20)), baseline={4: read}))
        self.assertIn("LARGE_CHANGE", codes(profile(p4(ratio=60))))  # +5 bins
        self.assertNotIn("LARGE_CHANGE", codes(profile(p4(ratio=58))))  # +3 bins
        text = " ".join(plan_for(profile(p4(offset_mv=-150))).warnings)
        self.assertIn("does not protect a whole-curve offset", text)

    def test_the_warnings_never_refuse(self) -> None:
        plan = plan_for(profile(p4(offset_mv=-700, ratio=58)))
        self.assertTrue(plan.allowed, plan.refusals)
        self.assertGreaterEqual(len([w for w in plan.warnings if w.startswith(("LARGE_CHANGE", "DEEP_WHOLE_CURVE"))]), 2)

    def test_revert_note_text(self) -> None:
        self.assertIn("a reboot restores the BIOS-programmed state", REVERT_NOTE)
        self.assertIn("a write cannot clear an OC ratio above the top fused ratio", REVERT_NOTE)
        self.assertIn("never one with a zero field", REVERT_NOTE)
        self.assertIn("after a raise the ratio first, then the voltage", REVERT_NOTE)  # frequency-first (D-016)
        self.assertTrue(REVERT_NOTE.isascii())


class RatioLimitIsTheCpusKindWideWord(unittest.TestCase):
    """G-141 / D-049: the plan's verdict for a ratio write uses the CPU's own ``0x01`` limit of the whole kind (the word of any CPU of the kind) -- the context the guard builds."""

    UNBOUND = policy_with(hw_ratio_max={"P": None, "E": None})
    P1 = CoreReadings(vf=VfWord(55, 0x544, False, 0), favored=0x3737, fused=0x538 << 8 | 55, granular=3)  # P1 (cpu 1)
    P0_LIMIT_59 = 0x100 | 59  # cmd 0x01 of P0 (cpu 0): 59x, ratio OC ok

    def plan(self, ratio: int, **kwargs: object):
        return build_plan(profile('kind = "P"\nindex = 1\nratio = %d' % ratio), TOPO, {1: self.P1}, per_core_enabled=True, policy=self.UNBOUND, **kwargs)

    def test_a_ratio_within_the_limit_read_on_another_cpu_of_the_kind_is_allowed(self) -> None:
        plan = self.plan(59, oc_cap_words={0: self.P0_LIMIT_59})
        refused = [(st.number, st.verdict.reason, st.verdict.detail) for st in plan.steps if not st.verdict.allowed]
        self.assertEqual(refused, [])

    def test_the_limit_is_still_a_limit(self) -> None:
        plan = self.plan(60, oc_cap_words={0: self.P0_LIMIT_59})
        self.assertTrue(any(not st.verdict.allowed and "59x" in st.verdict.detail and "cmd 0x01" in st.verdict.detail for st in plan.steps), [st.verdict for st in plan.steps])

    def test_an_e_word_does_not_answer_for_the_p_kind(self) -> None:
        plan = self.plan(59, oc_cap_words={8: self.P0_LIMIT_59})
        self.assertTrue(any(not st.verdict.allowed and "CPU_LIMIT_UNREAD" in st.verdict.detail for st in plan.steps), [st.verdict for st in plan.steps])

    def test_without_any_word_the_raise_is_refused_naming_the_read(self) -> None:
        plan = self.plan(59)
        self.assertTrue(any(not st.verdict.allowed and "CPU_LIMIT_UNREAD" in st.verdict.detail and "cmd 0x01" in st.verdict.detail for st in plan.steps))
        self.assertIn(StepKind.READ_OC_CAP, [st.kind for st in plan.steps])  # the plan itself reads the limit, for the apply that follows


class Shape(unittest.TestCase):
    def test_request_objects_are_guard_requests(self) -> None:
        plan = plan_for(profile(p4(offset_mv=-30)), misc_word=0)
        for s in plan.steps:
            req = s.request()
            self.assertIsInstance(req, Request)
            self.assertEqual((req.cpu, req.cmd, req.p1, req.p2), (s.cpu, s.cmd, 0x11 if s.cmd == 0x09 else 0, 0))


if __name__ == "__main__":
    unittest.main()
