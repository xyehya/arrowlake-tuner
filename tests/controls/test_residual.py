"""G-147 tests: a residual OC point is neutralised by the restore, recognised as equivalent, and a restore never lowers the favored ratio.

No hardware: the simulator behind the REAL guard (``tests/controls/_support``). The machine is ``SimConfig.measured()`` after a warm reboot that kept two E OC words
(``[MEASURED lnx 2026-10-13]``: EC1 ``0x00048C30`` = 48x + 1.137 V, EC2 ``0x00048C2E`` = 46x + 1.137 V; the accepted state has no OC point on either).
"""
from __future__ import annotations

import dataclasses
import tempfile
import unittest
from pathlib import Path

from arltuner.controls import StepKind, build_plan
from arltuner.controls import residual as R
from arltuner.guard.policy import fused_top_ratio
from arltuner.mailbox import VfWord
from arltuner.msr import SimConfig
from arltuner.profiles import load_profile
from arltuner.topology import arl_s_285k

from ._support import E_READINGS, DEFAULT_STEPS, e_core, make_session, policy_with, point_entry, point_profile, point_text, profile, readings_of, with_point_entries

TOPO = arl_s_285k()
EC1, EC2 = (12, 13, 14, 15), (16, 17, 18, 19)
FUSED_E = 0x48C2E  # fused top point of EC1..EC3: 46x, 1.137 V
EC1_WORD = VfWord(48, 0x48C, False, 0).to_u32()  # 0x00048C30
EC2_WORD = VfWord(46, 0x48C, False, 0).to_u32()  # 0x00048C2E
POINT6_OFFSET_RAW = VfWord.mv_to_offset_raw(-25)


def warm_reboot_config(**overrides: object) -> SimConfig:
    cfg = SimConfig.measured(per_point=True, **overrides)
    initial = dict(cfg.initial_vf)
    for cpu in EC1:
        initial[cpu] = VfWord.from_u32(EC1_WORD)
    for cpu in EC2:
        initial[cpu] = VfWord.from_u32(EC2_WORD)
    return dataclasses.replace(cfg, initial_vf=initial)


def accepted_profile():
    """The accepted state of the two clusters: only the 46x point at -25 mV; no OC ratio, no OC voltage (a points-only profile, as `profiles.accept` writes it)."""
    return point_profile(point_entry("E", 4, 6, -25, 46), point_entry("E", 8, 6, -25, 46), name="accepted")


class Case(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def session(self, **kwargs):
        kwargs.setdefault("config", warm_reboot_config())
        kwargs.setdefault("policy", with_point_entries(policy_with(max_ops=400, dwell=0.0, boot_ops=2000)))
        return make_session(self.tmp, **kwargs)


class TargetRule(unittest.TestCase):
    """``neutral_target``: below the accepted top point, one step from what the CPU holds, never above the fused voltage, never 0."""

    def test_reference_case_is_below_the_accepted_point_and_whole_mv(self) -> None:
        raw, mv = R.neutral_target(FUSED_E, -25, 0x48C, 205)
        top_point = 0x48C + POINT6_OFFSET_RAW  # 1.1112 V
        self.assertLess(raw, top_point)  # clamped up to the top point by the hardware
        self.assertLessEqual(raw, 0x48C)  # never above the fused top point
        self.assertEqual(raw, VfWord.v_to_raw(mv / 1000))
        self.assertEqual(mv, 1086)

    def test_a_deep_accepted_offset_moves_the_target_with_it(self) -> None:
        raw, _ = R.neutral_target(FUSED_E, -150, 0x48C, 400)  # "fused - 100 mV" would sit ABOVE this point
        self.assertLess(raw, 0x48C + VfWord.mv_to_offset_raw(-150))

    def test_a_positive_accepted_offset_never_lifts_it_above_the_fused_voltage(self) -> None:
        raw, _ = R.neutral_target(FUSED_E, 30, 0x48C, 400)
        self.assertLess(raw, 0x48C)

    def test_the_step_limit_bounds_the_move(self) -> None:
        raw, _ = R.neutral_target(FUSED_E, -25, 0x48C, 20)  # a 20 raw step: cannot reach 1.087 V in one word
        self.assertGreaterEqual(raw, 0x48C - 20)
        self.assertLessEqual(raw, 0x48C)

    def test_no_target_when_the_fused_voltage_is_unknown_or_unreachable(self) -> None:
        self.assertIsNone(R.neutral_target(0x2E, 0, 0, 205))  # fused voltage field 0
        self.assertIsNone(R.neutral_target(FUSED_E, 0, 0x7FF, 10))  # held far above: one step cannot get below the fused voltage


class Classify(unittest.TestCase):
    """``classify``: EQUIVALENT only for a residual OC point whose voltages are the accepted ones."""

    WANT = [0, 0x08, 0x0F, 0x17, 0x1E, 0x28, VfWord(46, 0, False, POINT6_OFFSET_RAW).to_u32()]
    GOT = [VfWord(48, 0x466, False, 0).to_u32(), *WANT[1:], VfWord(48, 0, False, 0).to_u32()]  # the OC point shows as one extra point

    def test_neutralised_residual_is_equivalent_and_the_extra_point_is_absorbed(self) -> None:
        self.assertEqual(R.classify(FUSED_E, self.WANT, self.GOT), {"ratio": 48, "extra_point": 7})

    def test_target_between_the_accepted_point_and_the_fused_voltage_is_not_equivalent(self) -> None:
        got = [VfWord(48, 0x48C - 13, False, 0).to_u32(), *self.GOT[1:]]  # 1.1245 V: above the accepted 1.1112 V point
        self.assertIsNone(R.classify(FUSED_E, self.WANT, got))
        got = [VfWord(48, VfWord.v_to_raw(1.120), False, 0).to_u32(), *self.GOT[1:]]
        self.assertIsNone(R.classify(FUSED_E, self.WANT, got))

    def test_a_target_at_or_below_the_accepted_point_voltage_is_equivalent(self) -> None:
        got = [VfWord(48, 0x48C + POINT6_OFFSET_RAW, False, 0).to_u32(), *self.GOT[1:]]
        self.assertIsNotNone(R.classify(FUSED_E, self.WANT, got))

    def test_the_offset_must_be_the_accepted_one(self) -> None:
        got = [VfWord(48, 0x466, False, -10).to_u32(), *self.GOT[1:]]
        self.assertIsNone(R.classify(FUSED_E, self.WANT, got))

    def test_a_different_point_or_extra_point_is_a_real_difference(self) -> None:
        got = list(self.GOT)
        got[6] = VfWord(46, 0, False, 0).to_u32()  # the 46x point lost its offset
        self.assertIsNone(R.classify(FUSED_E, self.WANT, got))
        got = list(self.GOT)
        got[7] = VfWord(49, 0, False, 0).to_u32()  # the extra point is not the OC ratio
        self.assertIsNone(R.classify(FUSED_E, self.WANT, got))

    def test_an_accepted_oc_point_is_never_residual(self) -> None:
        want = [VfWord(49, 0x4A0, False, 0).to_u32(), *self.WANT[1:]]
        self.assertIsNone(R.classify(FUSED_E, want, self.GOT))

    def test_without_the_fused_word_nothing_is_equivalent(self) -> None:
        self.assertIsNone(R.classify(None, self.WANT, self.GOT))

    def test_xtu_shaped_word_at_the_top_ratio_with_the_fused_target_is_equivalent_to_word_0(self) -> None:
        # the EC2 case: ratio 46 (= top, no OC point) with the fused voltage as target is the same as the accepted 0 -- but that is no residual OC point
        self.assertIsNone(R.classify(FUSED_E, self.WANT, [EC2_WORD, *self.WANT[1:]]))


class FavoredRatio(Case):
    """Deliverable 2: the planner never lowers the favored ratio as a side effect of a restore."""

    def plan(self, prof, cpus=(16,)):
        base = {c: readings_of(VfWord.from_u32(EC2_WORD), favored=0x2E30, fused=FUSED_E) for c in cpus}
        return build_plan(prof, TOPO, base, per_core_enabled=True, policy=policy_with(max_ops=400))

    def writes_of(self, plan, cmd):
        return [st for st in plan.steps if st.kind.is_write and st.cmd == cmd]

    def test_the_ec2_case_sends_no_0x1d_that_lowers_the_favored_ratio(self) -> None:
        plan = self.plan(profile(e_core(8, ratio=46)), cpus=EC2)  # the row restates 46 on a CPU whose 0x1C[7:0] reads 48
        self.assertEqual(self.writes_of(plan, 0x1D), [])
        self.assertEqual(self.writes_of(plan, 0x27), [])
        self.assertTrue(self.writes_of(plan, 0x11))
        self.assertTrue(any("no 0x1D" in w for w in plan.warnings))

    def test_a_deliberate_lowering_below_the_ratio_in_force_still_sends_its_0x1d(self) -> None:
        plan = self.plan(profile(e_core(8, ratio=44)), cpus=EC2)
        self.assertEqual([st.data for st in self.writes_of(plan, 0x1D)], [44] * 4)

    def test_an_oc_raise_sends_its_0x1d(self) -> None:
        plan = self.plan(profile(e_core(8, ratio=48)), cpus=EC2)
        self.assertEqual([st.data for st in self.writes_of(plan, 0x1D)], [48] * 4)

    def test_no_decrease_in_any_0x1d_of_a_restore_run(self) -> None:
        s = self.session()
        report = s.apply(profile(e_core(8, ratio=46)))
        self.assertTrue(report.ok, report.format_report())
        self.assertEqual([c for _, c, _ in s.write_requests() if c == 0x1D], [])
        self.assertEqual(s.sim.favored_word(16) & 0xFF, 48)
        self.assertEqual(s.sim.cpuinfo_max_khz(16), 4_900_000)


class Restore(Case):
    """Deliverables 1 + 5: the whole flow on the simulator -- warm reboot with residual words -> restore -> equivalent, voltages as accepted, favored unchanged."""

    def apply(self, s, **kwargs):
        return s.apply(accepted_profile(), neutralize_residual=True, **kwargs)

    def test_ec1_is_rewritten_with_a_low_target_and_ec2_is_left_alone(self) -> None:
        s = self.session()
        favored_before = {c: s.sim.favored_word(c) for c in (*EC1, *EC2)}
        report = self.apply(s)
        self.assertTrue(report.ok, report.format_report())
        for cpu in EC1:
            word = s.sim.vf(cpu)
            self.assertEqual(word.ratio, 48)  # the OC ratio is kept (it cannot be removed)
            self.assertEqual(word.offset_raw, 0)  # the accepted whole-curve offset
            self.assertTrue(0 < word.v_target_raw < 0x48C, hex(word.v_target_raw))
            self.assertLess(word.v_target_raw, 0x48C + POINT6_OFFSET_RAW)  # below the accepted top point -> the hardware clamps it up
        self.assertEqual(s.sim.oc_point_voltage_raw(12), 0x48C + POINT6_OFFSET_RAW)  # EC1 48x runs the ACCEPTED voltage, not 1.137 V
        for cpu in EC2:
            self.assertEqual(s.sim.vf(cpu).to_u32(), EC2_WORD)  # ratio 46 = top: no OC point, nothing to neutralise
        self.assertEqual({c: s.sim.favored_word(c) for c in (*EC1, *EC2)}, favored_before)
        self.assertEqual([c for _, c, _ in s.write_requests() if c in (0x1D, 0x27)], [])

    def test_no_zero_ratio_is_ever_sent_and_no_unit_but_the_residual_ones_is_written(self) -> None:
        s = self.session()
        self.apply(s)
        for cpu, cmd, data in s.write_requests():
            if cmd == 0x11:
                self.assertNotEqual(VfWord.from_u32(data).ratio, 0, (cpu, hex(data)))
        core_writes = {cpu for cpu, cmd, data in s.write_requests() if cmd == 0x11 and VfWord.from_u32(data).v_target_raw}  # point words carry target 0
        self.assertEqual(core_writes, set(EC1))

    def test_the_scan_reads_two_words_per_unit_and_is_budgeted(self) -> None:
        s = self.session()
        report = self.apply(s)
        scan = [r for r in report.steps if "restore scan" in r.step.purpose]
        self.assertEqual(len(scan), 2 * 12)  # 8 P + 4 E clusters, none of them has an accepted OC ratio
        self.assertEqual({r.step.cmd for r in scan}, {0x10, 0x09})

    def test_the_result_compares_equivalent_and_a_second_restore_writes_nothing(self) -> None:
        s = self.session()
        self.apply(s)
        want = [0, *(VfWord(r, 0, False, 0).to_u32() for r in (8, 15, 23, 30, 40)), VfWord(46, 0, False, POINT6_OFFSET_RAW).to_u32()]
        got = [s.sim.vf(12).to_u32(), *(s.sim.vf(12, 0, p).to_u32() for p in range(1, 8))]
        verdict = R.classify(FUSED_E, want, got)
        self.assertEqual(verdict, {"ratio": 48, "extra_point": 7}, got)
        s.sim.clear_log()
        again = self.apply(s)
        self.assertTrue(again.ok, again.format_report())
        oc_writes = [cpu for cpu, cmd, data in s.write_requests() if cmd == 0x11 and VfWord.from_u32(data).v_target_raw]
        self.assertEqual(oc_writes, [])  # already neutral: only the point words go out again

    def test_off_by_default(self) -> None:
        s = self.session()
        report = s.apply(accepted_profile())
        self.assertTrue(report.ok, report.format_report())
        self.assertFalse([r for r in report.steps if "restore scan" in r.step.purpose])
        self.assertEqual(s.sim.vf(12).to_u32(), EC1_WORD)  # untouched

    def test_a_unit_the_accepted_state_gives_an_oc_ratio_is_not_scanned(self) -> None:
        s = self.session()
        prof = load_profile(point_text(point_entry("E", 4, 6, -25, 46), entries=(e_core(4, ratio=48, oc_voltage_mv=1130),), name="accepted"))
        report = s.apply(prof, neutralize_residual=True)
        self.assertTrue(report.ok, report.format_report())
        scan = [r.step.cpu for r in report.steps if "restore scan" in r.step.purpose]
        self.assertNotIn(12, scan)
        self.assertEqual(len(scan), 2 * 11)

    def test_chain_rows_before_the_last_profile_count_as_accepted(self) -> None:
        s = self.session()
        earlier = profile(e_core(4, ratio=48, oc_voltage_mv=1150), name="earlier")
        report = s.apply(accepted_profile(), neutralize_residual=True, accepted_before=(earlier,))
        scan = [r.step.cpu for r in report.steps if "restore scan" in r.step.purpose]
        self.assertNotIn(12, scan)

    def test_the_policy_ratio_ceiling_is_never_widened(self) -> None:
        pol = with_point_entries(policy_with(max_ops=400, dwell=0.0, boot_ops=2000, ratio_max={"P": None, "E": 47}))
        s = self.session(policy=pol)
        report = self.apply(s)
        self.assertTrue(report.ok, report.format_report())
        self.assertEqual(s.sim.vf(12).to_u32(), EC1_WORD)  # left as it is
        self.assertTrue(any("ratio_max" in w for w in report.warnings), report.warnings)

    def test_a_step_limit_that_cannot_reach_the_goal_still_moves_toward_it_by_one_step(self) -> None:
        pol = with_point_entries(policy_with(max_ops=400, dwell=0.0, boot_ops=2000, **DEFAULT_STEPS))
        s = self.session(policy=pol)
        report = self.apply(s)
        self.assertTrue(report.ok, report.format_report())
        word = s.sim.vf(12)
        self.assertEqual(word.ratio, 48)
        self.assertGreaterEqual(word.v_target_raw, 0x48C - 21)  # one 20 mV step
        self.assertLess(word.v_target_raw, 0x48C)

    def test_the_scan_reads_count_against_the_session_limit(self) -> None:
        from arltuner.controls import NeedsHigherLimit

        pol = with_point_entries(policy_with(max_ops=30, dwell=0.0, boot_ops=2000))
        s = self.session(policy=pol)
        with self.assertRaises(NeedsHigherLimit):
            self.apply(s)
        self.assertEqual(s.sim.requests, [])  # nothing was sent
        report = s.apply(accepted_profile())  # without the scan the same plan fits
        self.assertTrue(report.ok, report.format_report())

    def test_an_unreadable_unit_by_policy_is_skipped_not_fatal(self) -> None:
        pol = with_point_entries(policy_with(max_ops=400, dwell=0.0, boot_ops=2000, cpus=frozenset(range(8, 24))))
        s = self.session(policy=pol)
        report = self.apply(s)
        self.assertTrue(report.ok, report.format_report())
        scan = {r.step.cpu for r in report.steps if "restore scan" in r.step.purpose}
        self.assertEqual(scan, {8, 12, 16, 20})


class StickyOcRatio(Case):
    def test_the_simulator_refuses_to_write_the_oc_ratio_back_to_zero(self) -> None:
        s = self.session(config=warm_reboot_config(), reject_zero_fields=False)
        from arltuner.mailbox import Cmd

        resp = s.sim._execute(12, int(Cmd.VOLTAGE_FREQ_WRITE), 0, 0, 0)
        self.assertNotEqual(resp[0], 0)
        self.assertEqual(s.sim.vf(12).to_u32(), EC1_WORD)  # the word stays; only a cold boot (a new simulator) clears it
        self.assertEqual(warm_reboot_config().initial_vf[12].to_u32(), EC1_WORD)

    def test_the_clamp_peek_returns_zero_without_an_oc_point(self) -> None:
        s = self.session()
        self.assertEqual(s.sim.oc_point_voltage_raw(16), 0)  # EC2: 46x = top
        self.assertEqual(s.sim.oc_point_voltage_raw(12), 0x48C)  # EC1: the target 1.137 V = the fused top point (no point offset yet)


class CapRule(unittest.TestCase):
    """``controls.cpufreq``: the one cap rule of the helper's tune.apply and the CLI's apply."""

    def test_the_attribute_names_are_the_helpers(self) -> None:
        from arltuner.controls import cpufreq
        from arltuner.helper import ops_measure

        self.assertEqual((cpufreq.CAP_ATTR, cpufreq.MAX_ATTR), (ops_measure.CAP_ATTR, ops_measure.MAX_ATTR))

    def test_follow_cap_decisions(self) -> None:
        from arltuner.controls.cpufreq import follow_cap

        self.assertEqual(follow_cap(4_900_000, 5_000_000, 4_900_000), 5_000_000)  # cap at the old maximum follows it up
        self.assertEqual(follow_cap(5_000_000, 4_900_000, 5_000_000), 4_900_000)  # ... and down
        self.assertEqual(follow_cap(4_900_000, 5_000_000, 4_000_000), None)  # a cap the user lowered is never raised
        self.assertEqual(follow_cap(5_000_000, 4_700_000, 4_900_000), 4_700_000)  # a cap above a lowered maximum is lowered
        self.assertEqual(follow_cap(5_000_000, 4_700_000, 4_000_000), None)
        self.assertEqual(follow_cap(4_900_000, 4_900_000, 4_900_000), None)  # nothing moved
        self.assertEqual(follow_cap(None, 5_000_000, 4_900_000), None)


class NoteText(unittest.TestCase):
    def test_the_note_is_the_one_every_surface_shows(self) -> None:
        self.assertEqual(R.RESIDUAL_NOTE, "residual OC point (cleared by a cold boot); voltages as accepted")
        self.assertEqual(fused_top_ratio(FUSED_E), 46)
        self.assertTrue(StepKind.READ_VF)


if __name__ == "__main__":
    unittest.main()
