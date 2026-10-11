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
    base = {4: P4_READINGS} if baseline is None else {c: readings_of(v) if isinstance(v, VfWord) else v for c, v in baseline.items()}
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
    return by_cmd[0x11], by_cmd[0x1D], by_cmd[0x27]


class StepSequence(unittest.TestCase):
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
                (StepKind.WRITE_FAVORED, 0x1D, 4, 0, 0),
                (StepKind.WRITE_GRANULAR, 0x27, 4, 0, 0),
            ],
        )
        self.assertEqual([s.number for s in plan.steps], list(range(1, 10)))
        self.assertEqual((plan.guard_operations, plan.required_operations), (9, 13))  # 9 operations + 4 read-backs (0x15, 0x11, 0x1D, 0x27)
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
        def word(prof):
            return next(s for s in plan_for(prof).steps if s.kind is StepKind.WRITE_VF).after

        self.assertEqual(word(profile(p4(ratio=50))), P4_BASELINE.with_(ratio=50))
        w = word(profile(p4(oc_voltage_mv=1300)))
        self.assertEqual(w, P4_BASELINE.with_(v_target_raw=VfWord.v_to_raw(1.3)))  # 1331: integer mV snaps to 1/1024 V
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
        self.assertEqual([s.cmd for s in enabled.steps], [0x14, 0x10, 0x1C, 0x09, 0x26, 0x11, 0x1D, 0x27])
        self.assertEqual((enabled.guard_operations, enabled.read_back_requests, enabled.required_operations), (8, 3, 11))
        clear = plan_for(profile(p4(offset_mv=-30)), per_core_enabled=False)
        self.assertEqual([s.cmd for s in clear.steps], [0x14, 0x10, 0x1C, 0x09, 0x26, 0x15, 0x11, 0x1D, 0x27])
        unknown = plan_for(profile(p4(offset_mv=-30)), per_core_enabled=None)
        step = unknown.steps[5]
        self.assertEqual(step.cmd, 0x15)
        self.assertTrue(step.conditional)
        self.assertIsNone(step.data)  # old|0x8 is not known yet
        self.assertEqual((unknown.guard_operations, unknown.required_operations), (9, 13))  # worst case: + one read-back per write
        with self.assertRaises(ValueError):
            step.request()

    def test_0x15_data_is_old_or_8_when_the_word_is_known(self) -> None:
        plan = plan_for(profile(p4(offset_mv=-30)), per_core_enabled=False, misc_word=0x1)
        self.assertEqual(plan.steps[5].data, 0x9)
        self.assertEqual(plan.steps[5].request().data, 0x9)
        self.assertFalse(plan.steps[5].conditional)
        derived = plan_for(profile(p4(offset_mv=-30)), per_core_enabled=None, misc_word=0x8)
        self.assertTrue(derived.per_core_enabled)
        self.assertEqual([s.cmd for s in derived.steps], [0x14, 0x10, 0x1C, 0x09, 0x26, 0x11, 0x1D, 0x27])
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
        self.assertEqual(write_words(plan, 0), (0xFF600039, 0x39, 0x3))  # G-120: P0's OC ratio 57 = top fused ratio, no OC point: ratio | offset << 21, target 0 [MEASURED lnx 2026-10-11]
        self.assertTrue(plan.allowed, plan.refusals)

    def test_cpu0_minus_15_mv_is_xtus_word(self) -> None:
        self.assertEqual(write_words(self.plan(profile(p_core(0, -15)), [0]), 0), (0xFE200039, 0x39, 0x3))

    def test_cpu0_revert_word_keeps_the_fused_v_target_not_the_zero_one(self) -> None:
        plan = self.plan(profile(p_core(0, 0)), [0])
        self.assertEqual(write_words(plan, 0), (0x0005B439, 0x39, 0x3))  # XTU's revert word; the baseline 0x39 is NOT rewritten
        self.assertTrue(plan.allowed, plan.refusals)

    def test_cpu1_ratio_comes_from_0x1c_and_v_target_from_0x09(self) -> None:
        plan = self.plan(profile(p_core(1, -5)), [1])
        self.assertEqual(write_words(plan, 1), (0xFF600037, 0x37, 0x3))  # ratio 0x37 = 55 (0x1C), not 57 (0x09); no OC point -> V target 0
        self.assertEqual(write_words(self.plan(profile(p_core(1, 0)), [1]), 1), (0x00053837, 0x37, 0x3))
        self.assertTrue(plan.allowed, plan.refusals)

    def test_cpu1_step_list_equals_xtus_with_and_without_the_0x15(self) -> None:
        enabled = self.plan(profile(p_core(1, -5)), [1], per_core_enabled=True)
        self.assertEqual([s.cmd for s in enabled.steps], [0x14, 0x10, 0x1C, 0x09, 0x26, 0x11, 0x1D, 0x27])
        self.assertEqual([(s.cmd, s.p1) for s in enabled.steps if s.cmd == 0x09], [(0x09, 0x11)])
        clear = self.plan(profile(p_core(1, -5)), [1], per_core_enabled=False)
        self.assertEqual([s.cmd for s in clear.steps], [0x14, 0x10, 0x1C, 0x09, 0x26, 0x15, 0x11, 0x1D, 0x27])

    def test_no_word_with_a_zero_ratio_is_ever_planned_and_a_zero_v_target_only_without_an_oc_point(self) -> None:
        for cpu in range(24):
            prof = profile(p_core(cpu, -5)) if cpu < 8 else profile(e_core(cpu - 8, -5))
            cpus = [cpu] if cpu < 8 else list(range(8 + 4 * ((cpu - 8) // 4), 12 + 4 * ((cpu - 8) // 4)))
            plan = self.plan(prof, cpus)
            for c in cpus:
                w = VfWord.from_u32(write_words(plan, c)[0])
                self.assertNotEqual((w.ratio, w.v_target_raw == 0), (0, True), c)
                self.assertGreater(w.ratio, 0, c)
                if cpu < 8:  # G-120: ratio <= 57 = no OC point -> the kept word has no target
                    self.assertEqual(w.v_target_raw, 0, c)
                else:  # an E word takes ratio 0x1C[7:0] = 48 > 46 = the top fused ratio: it IS an OC point (XTU's shape), so it carries the fused target
                    self.assertGreater(w.v_target_raw, 0, c)
                self.assertFalse(w.static)
            self.assertTrue(plan.allowed, (cpu, plan.refusals))

    def test_e_cluster_words_equal_xtus_for_all_16_cores(self) -> None:
        prof = profile(e_core(0, offset_mv=-5), e_core(4, offset_mv=-5), e_core(8, offset_mv=-5), e_core(12, offset_mv=-5))
        plan = self.plan(prof, range(8, 24))
        for cpu in range(8, 12):
            self.assertEqual(write_words(plan, cpu), (0xFF64A030, 0x30, 0x3), cpu)
        for cpu in range(12, 24):
            self.assertEqual(write_words(plan, cpu), (0xFF648C30, 0x30, 0x3), cpu)
        self.assertTrue(plan.allowed, plan.refusals)
        self.assertEqual((plan.guard_operations, plan.required_operations), (1 + 7 * 16, 1 + 7 * 16 + 3 * 16))
        reverts = profile(e_core(0, offset_mv=0), e_core(4, offset_mv=0), e_core(8, offset_mv=0), e_core(12, offset_mv=0))
        rplan = self.plan(reverts, range(8, 24))
        self.assertEqual({write_words(rplan, c)[0] for c in range(8, 12)}, {0x0004A030})
        self.assertEqual({write_words(rplan, c)[0] for c in range(12, 24)}, {0x00048C30})

    def test_a_baseline_word_is_used_when_non_zero_and_a_missing_reference_is_refused_not_invented(self) -> None:
        # a core whose 0x10 word carries ratio 50: XTU keeps it (ratio = the non-zero baseline ratio, not 0x1C[7:0])
        r = dataclasses.replace(measured_readings(1), vf=VfWord(50, 0, False, 0))
        plan = build_plan(profile(p_core(1, -5)), TOPO, {1: r}, per_core_enabled=True, policy=POLICY)
        self.assertEqual(VfWord.from_u32(write_words(plan, 1)[0]).ratio, 50)
        self.assertEqual(write_words(plan, 1)[1], 50)
        # no 0x1C ratio and no baseline ratio: the word would carry ratio 0 -> predicted refusal, nothing made up
        none = dataclasses.replace(measured_readings(1), favored=0)
        refused = build_plan(profile(p_core(1, -5)), TOPO, {1: none}, per_core_enabled=True, policy=POLICY)
        self.assertFalse(refused.allowed)
        self.assertTrue(any("ratio 0" in r.detail for r in refused.refusals), refused.refusals)
        refused = build_plan(profile(e_core(0, -5)), TOPO, {c: dataclasses.replace(measured_readings(c), fused=0x2E) for c in range(8, 12)}, per_core_enabled=True, policy=POLICY)
        self.assertTrue(any("v_target_raw 0" in r.detail for r in refused.refusals), refused.refusals)

    def test_xtu_word_helper(self) -> None:
        self.assertEqual(xtu_word(VfWord.from_u32(0x39), 0x3939, 0x5B439).to_u32(), 0x0005B439)
        self.assertEqual(xtu_word(VfWord.from_u32(0x0), 0x3737, 0x53839).to_u32(), 0x00053837)
        self.assertEqual(xtu_word(VfWord.from_u32(0x0), 0x2E30, 0x4A02E).to_u32(), 0x0004A030)

    def test_effective_offsets_are_shown_in_the_plan_text(self) -> None:
        text = format_plan(self.plan(profile(p_core(0, -5), e_core(0, offset_mv=-5)), [0, 8, 9, 10, 11]))
        self.assertIn("runtime offset -4.88 mV, effective -54.88 mV (assumed BIOS offset -50 mV)", text)  # P core
        self.assertIn("runtime offset -4.88 mV, effective -4.88 mV (assumed BIOS offset +0 mV)", text)  # E core
        self.assertIn("effective offset = assumed BIOS offset (P -50 mV, E +0 mV", text)
        self.assertIn("-100 mV (D-015)", text)


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
        self.assertEqual((plan.guard_operations, plan.required_operations), (1 + 4 * 4 + 1 + 4 * 3, 1 + 4 * 4 + 1 + 4 * 3 + 1 + 4 * 3))
        self.assertEqual({s.cpu for s in plan.steps if s.kind is StepKind.WRITE_FAVORED}, {20, 21, 22, 23})

    def test_entries_of_one_cluster_are_deduplicated(self) -> None:
        both = profile(e_core(12, offset_mv=-20), e_core(13, offset_mv=-20))
        self.assertEqual([t.cpu for t in resolve_targets(both, TOPO)], [20, 21, 22, 23])

    def test_mixed_p_and_e_profile(self) -> None:
        prof = profile(p4(offset_mv=-30), e_core(0, offset_mv=-20))
        targets = resolve_targets(prof, TOPO)
        self.assertEqual([t.cpu for t in targets], [4, 8, 9, 10, 11])
        base = {4: P4_READINGS, **{c: E_READINGS for c in (8, 9, 10, 11)}}
        plan = plan_for(prof, base)
        self.assertEqual((plan.guard_operations, plan.required_operations), (1 + 5 * 4 + 1 + 5 * 3, 1 + 5 * 4 + 1 + 5 * 3 + 1 + 5 * 3))
        self.assertEqual([s.cmd for s in plan.steps], [0x14] + [0x10, 0x1C, 0x09, 0x26] * 5 + [0x15] + [0x11, 0x1D, 0x27] * 5)
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
        self.assertEqual(plan.required_operations, 13)


class Counting(unittest.TestCase):
    def test_count_operations(self) -> None:
        # every WRMSR: the 0x14 read + per CPU (4 reads + 3 writes) + the 0x15 write unless known set + ONE READ-BACK PER WRITE (T-022)
        self.assertEqual(count_operations(1, True), 1 + 4 + 3 + 3)
        self.assertEqual(count_operations(1, False), 1 + 4 + 4 + 4)
        self.assertEqual(count_operations(1, None), 13)  # unknown = worst case (the 0x15 write and its read-back included)
        self.assertEqual(count_operations(4, False), 17 + 13 + 13)
        self.assertEqual(count_operations(5, True), 21 + 15 + 15)
        self.assertEqual(count_operations(16, True), 65 + 48 + 48)
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
        self.assertEqual(count_operations(ratio_only, True), base)  # a ratio alone: one 0x11, no 0x28
        self.assertEqual(count_operations(volts_only, True), base + 1)  # + the 0x28 read, still one 0x11
        self.assertEqual(count_operations(both, True), base + 1 + 2)  # + the 0x28 read + a second 0x11 and its read-back
        self.assertEqual(count_operations(both, None), base + 1 + 2 + 2)  # unknown 0x15 state: + the write and its read-back

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
        self.assertEqual((plan.required_operations, plan.allowed_operations), (13, 1))
        with self.assertRaises(NeedsHigherLimit) as ctx:
            plan.require_allowed()
        self.assertEqual((ctx.exception.required, ctx.exception.allowed), (13, 1))
        with self.assertRaises(NeedsHigherLimit):
            preflight(profile(p4(offset_mv=-30)), TOPO, default_m1_policy())

    def test_exact_limit_is_enough(self) -> None:
        # 9 guard operations + 4 read-backs = 13 WRMSRs: the budget counts the read-backs although the guard's own counter does not
        self.assertTrue(plan_for(profile(p4(offset_mv=-30)), policy=policy_with(max_ops=13)).allowed)
        self.assertFalse(plan_for(profile(p4(offset_mv=-30)), policy=policy_with(max_ops=12)).allowed)
        self.assertFalse(plan_for(profile(p4(offset_mv=-30)), policy=policy_with(max_ops=9)).allowed)  # enough for the guard, not for the budget
        preflight(profile(p4(offset_mv=-30)), TOPO, policy_with(max_ops=13))
        with self.assertRaises(NeedsHigherLimit) as ctx:
            preflight(profile(p4(offset_mv=-30)), TOPO, policy_with(max_ops=12))
        self.assertEqual((ctx.exception.required, ctx.exception.allowed), (13, 12))

    def test_the_default_per_boot_cap_cannot_cover_an_e_cluster(self) -> None:
        pol = dataclasses.replace(default_m1_policy(), max_operations_per_session=60)  # per-boot cap stays 12
        with self.assertRaises(NeedsBootBudget) as ctx:
            preflight(profile(e_core(12, offset_mv=-20)), TOPO, pol)
        self.assertEqual((ctx.exception.required, ctx.exception.remaining), (43, 12))  # 30 operations + 13 read-backs

    def test_preflight_counts_the_per_boot_budget_before_anything_is_sent(self) -> None:
        pol = policy_with(max_ops=60, boot_ops=20)  # a plan for one P-core needs 13 WRMSRs (9 operations + 4 read-backs)
        preflight(profile(p4(offset_mv=-30)), TOPO, pol, boot_ops_used=7)  # exactly enough: 20 - 7 == 13
        with self.assertRaises(NeedsBootBudget) as ctx:
            preflight(profile(p4(offset_mv=-30)), TOPO, pol, boot_ops_used=8)  # one short
        self.assertEqual((ctx.exception.required, ctx.exception.remaining, ctx.exception.used, ctx.exception.cap), (13, 12, 8, 20))
        with self.assertRaises(NeedsBootBudget) as ctx:  # an already exhausted budget never goes negative
            preflight(profile(p4(offset_mv=-30)), TOPO, pol, boot_ops_used=99)
        self.assertEqual(ctx.exception.remaining, 0)
        preflight(profile(p4(offset_mv=-30)), TOPO, pol)  # default: nothing used yet
        for bad in (-1, 1.5, True, "3"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                preflight(profile(p4(offset_mv=-30)), TOPO, pol, boot_ops_used=bad)  # type: ignore[arg-type]

    def test_estimated_min_seconds_is_dwell_times_operations(self) -> None:
        self.assertEqual(plan_for(profile(p4(offset_mv=-30)), policy=policy_with(dwell=60.0)).estimated_min_seconds, 540.0)
        self.assertEqual(plan_for(profile(p4(offset_mv=-30)), policy=policy_with(dwell=0.0)).estimated_min_seconds, 0.0)
        self.assertEqual(plan_for(profile(p4(offset_mv=-30)), per_core_enabled=True, policy=policy_with(dwell=10.0)).estimated_min_seconds, 80.0)


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
        # raw 1434 > 0x544 = 1348, the no-raise ceiling of the shipped policy; step 8: an OC-point voltage adds the 0x28 read before the writes
        self.assertIn("v_target_raw", self.refused(profile(p4(oc_voltage_mv=1400)), step=8).detail)

    def test_ratio_above_baseline(self) -> None:
        self.assertIn("ratio", self.refused(profile(p4(ratio=57))).detail)

    def test_offset_below_the_policy_minimum(self) -> None:
        self.assertIn("offset_raw", self.refused(profile(p4(offset_mv=-150))).detail)  # raw -154 < -102

    def test_offset_boundary_on_a_p_core_is_the_d015_runtime_floor(self) -> None:
        self.assertTrue(plan_for(profile(p4(offset_mv=-50))).allowed)  # raw -51 == the P runtime floor (-100 effective - -50 BIOS)
        detail = self.refused(profile(p4(offset_mv=-51))).detail  # raw -52
        self.assertIn("-51", detail)
        self.assertIn("D-015", detail)

    def test_offset_boundary_on_an_e_core_is_minus_100_mv(self) -> None:
        base = {c: E_READINGS for c in (20, 21, 22, 23)}
        self.assertTrue(plan_for(profile(e_core(12, offset_mv=-100)), base).allowed)  # raw -102
        plan = plan_for(profile(e_core(12, offset_mv=-101)), base)  # raw -103: refused on every core of the cluster
        self.assertEqual(len(plan.refusals), 4)
        self.assertEqual({r.cmd for r in plan.refusals}, {0x11})

    def test_the_floor_follows_the_policy_values(self) -> None:
        lenient = dataclasses.replace(POLICY, assumed_bios_offset_mv={"P": 0, "E": 0})
        self.assertTrue(plan_for(profile(p4(offset_mv=-100)), policy=lenient).allowed)  # no BIOS offset assumed: -100 mV runtime
        strict = dataclasses.replace(POLICY, assumed_bios_offset_mv={"P": -80, "E": 0})
        self.assertTrue(plan_for(profile(p4(offset_mv=-20)), policy=strict).allowed)  # floor -100 - -80 = -20 mV
        self.refused(profile(p4(offset_mv=-21)), policy=strict)

    def test_positive_offset_is_refused(self) -> None:
        self.refused(profile(p4(offset_mv=10)))

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
        self.assertEqual(len(plan.refusals), 9)  # every step is on cpu 4
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
        for cmd, step_number in ((0x1D, 8), (0x27, 9)):  # a write the policy does not list: caught by pre-flight too
            with self.subTest(write=hex(cmd)):
                policy = without(cmd)
                self.assertEqual([r.step_number for r in plan_for(profile(p4(offset_mv=-30)), policy=policy).refusals], [step_number])
                with self.assertRaises(PlanRefused) as ctx:
                    preflight(profile(p4(offset_mv=-30)), TOPO, policy)
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
        self.assertIn("zero ratio or V target", XTU_SHAPE_CAVEAT)
        self.assertIn("[MEASURED win 2026-10-01]", XTU_SHAPE_CAVEAT)
        self.assertTrue(XTU_SHAPE_CAVEAT.isascii())
        self.assertNotIn("not modelled", XTU_SHAPE_CAVEAT)  # the T-014 ratio caveat is gone

    def test_cluster_wide_warning_for_e_cores(self) -> None:
        base = {c: E_READINGS for c in (20, 21, 22, 23)}
        warnings = plan_for(profile(e_core(12, offset_mv=-20)), base).warnings
        self.assertTrue(any("cluster" in w and "UNVERIFIED" in w for w in warnings))
        self.assertFalse(any("cluster" in w for w in plan_for(profile(p4(offset_mv=-30))).warnings))

    def test_bios_offset_note_appears_with_an_offset_only(self) -> None:
        self.assertTrue(any("assumed BIOS offset" in w and "D-015" in w for w in plan_for(profile(p4(offset_mv=-30))).warnings))
        self.assertFalse(any("assumed BIOS offset" in w for w in plan_for(profile(p4(ratio=50))).warnings))

    def test_revert_note_text(self) -> None:
        self.assertIn("a reboot restores the BIOS-programmed state", REVERT_NOTE)
        self.assertIn("baseline of 0x0 cannot be restored by a write", REVERT_NOTE)
        self.assertIn("never a word with a zero field", REVERT_NOTE)
        self.assertIn("after a raise the ratio first, then the voltage", REVERT_NOTE)  # frequency-first (D-016)
        self.assertTrue(REVERT_NOTE.isascii())


class Shape(unittest.TestCase):
    def test_request_objects_are_guard_requests(self) -> None:
        plan = plan_for(profile(p4(offset_mv=-30)), misc_word=0)
        for s in plan.steps:
            req = s.request()
            self.assertIsInstance(req, Request)
            self.assertEqual((req.cpu, req.cmd, req.p1, req.p2), (s.cpu, s.cmd, 0x11 if s.cmd == 0x09 else 0, 0))


if __name__ == "__main__":
    unittest.main()
