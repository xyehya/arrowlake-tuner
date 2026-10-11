"""T-021 golden tests: ``apply_profile`` against ``SimConfig.measured()`` (the machine as MEASURED on Windows) through the REAL guard.

Every sequence below is one XTU sent on this machine, ``[MEASURED win 2026-10-01]``, docs/research/win-xtu-live-trace.md: S-04 / S-05
(CPU 0 -5 mV and back), S-04c (CPU 0 -15 mV), S-04d (CPU 1 -5 mV and back; baseline word 0x0), S-06 / S-07 (all 16 E-cores -5 mV and back).
The simulator runs with ``reject_zero_fields=True``: a plan that wrote a 0x11 with a zero ratio / V target would fail here.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from arltuner.controls import NeedsRamp, PlanRefused, StepKind, StopKind
from arltuner.guard import ENV_FLAG, AuditLog, Confirmation, Request, WriteGuard
from arltuner.mailbox import VfWord
from arltuner.msr import MailboxTransport, SimConfig, SimMsrPort

from ._support import DEFAULT_STEPS, e_core, make_session, policy_with, profile

XTU_READS = [0x14, 0x10, 0x1C, 0x09, 0x26]  # misc (bit 3 already set: no 0x15), then the four reads of the core


def p_core(index: int, offset_mv: int) -> str:
    """XTU's per-core entry: it also writes the ratio (0x1D) and the granular word (0x27), so the golden sequences below carry the core's favored ratio (P0/P3 57, others 55).
    G-122: an offset-only profile sends the 0x11 alone; see ``ECurve`` / ``OffsetOnly``."""
    return f'kind = "P"\nindex = {index}\noffset_mv = {offset_mv}\nratio = {57 if index in (0, 3) else 55}'


class MeasuredCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def session(self, **kwargs):
        kwargs.setdefault("config", SimConfig.measured())
        kwargs.setdefault("policy", policy_with(max_ops=400, dwell=0.0, boot_ops=2000))
        return make_session(self.tmp, **kwargs)

    @staticmethod
    def writes(s) -> list[tuple[int, int, int]]:
        return s.write_requests()

    def run_revert_plan(self, s, report, cpus) -> None:
        """Send the report's revert requests in a NEW guard session (as the human would), after that session's own baseline reads."""
        sim: SimMsrPort = s.sim
        g = WriteGuard(
            MailboxTransport(sim, sleep=s.fake.sleep, clock=s.fake.clock),
            policy_with(max_ops=400, dwell=0.0, boot_ops=2000),
            AuditLog(self.tmp / "revert-audit.jsonl", fsync=False),
            session_token="rv",
            boot_id_fn=lambda: "test-boot",
            env={ENV_FLAG: "1"},
            lock_path=self.tmp / "revert.lock",
            clock=s.fake.clock,
        )
        confirm = Confirmation("rv", "tester")
        for cpu in cpus:
            for cmd, p1 in ((0x10, 0), (0x1C, 0), (0x09, 0x11), (0x26, 0)):
                g.execute(Request(cpu, cmd, p1, 0, 0, "baseline"), confirm)
        for req in report.revert_plan:
            self.assertIs(g.execute(req, confirm).verified, True, req)


class Cpu0(MeasuredCase):
    def test_minus_5_mv_is_xtus_sequence_word_for_word(self) -> None:
        s = self.session()
        report = s.apply(profile(p_core(0, -5)))
        self.assertTrue(report.ok, report.format_report())
        self.assertEqual(report.operations, 8)
        self.assertEqual([r.step.cmd for r in report.steps], XTU_READS + [0x11, 0x1D, 0x27])
        self.assertEqual(self.writes(s), [(0, 0x11, 0xFF600039), (0, 0x1D, 0x39), (0, 0x27, 0x3)])  # S-04, in XTU's per-core order
        self.assertNotIn(0x15, [cmd for _, cmd, _ in s.requests()])  # 0x14 bit 3 is set: XTU skips the 0x15, so do we
        self.assertEqual(s.sim.vf(0).to_u32(), 0xFF600039)
        self.assertEqual((s.sim.favored_word(0), s.sim.granular_word(0)), (0x3939, 3))
        self.assertEqual(report.zero_baseline_cpus, ())  # G-124: its baseline 0x39 (57x, no V target) is the word the hardware keeps without an OC point: restorable
        self.assertTrue(all(r.verified for r in report.steps if r.step.kind.is_write))

    def test_minus_15_mv_is_xtus_word(self) -> None:
        s = self.session()
        report = s.apply(profile(p_core(0, -15)))
        self.assertTrue(report.ok, report.format_report())
        self.assertEqual(self.writes(s)[0], (0, 0x11, 0xFE200039))  # S-04c
        self.assertEqual(s.sim.vf(0).to_u32(), 0xFE200039)

    def test_reverting_with_offset_0_writes_xtus_revert_word_not_the_baseline(self) -> None:
        s = self.session()
        s.apply(profile(p_core(0, -5)))
        s.sim.clear_log()
        report = s.apply(profile(p_core(0, 0)))
        self.assertTrue(report.ok, report.format_report())
        self.assertEqual(self.writes(s), [(0, 0x11, 0x00000039), (0, 0x1D, 0x39), (0, 0x27, 0x3)])  # S-05 shape; G-124: XTU's 0x0005B439 minus the target the hardware drops
        self.assertEqual(s.sim.vf(0).to_u32(), 0x00000039)  # G-124: the cold-boot word itself (target 0)

    def test_the_revert_plan_of_the_report_is_xtus_revert_and_runs_in_a_new_session(self) -> None:
        s = self.session()
        report = s.apply(profile(p_core(0, -15)))
        self.assertEqual([(r.cmd, r.data) for r in report.revert_plan], [(0x11, 0x00000039), (0x1D, 0x39), (0x27, 3)])
        text = report.format_report()
        self.assertIn("a reboot restores the BIOS-programmed state; a baseline of 0x0 cannot be restored by a write", text)
        self.assertNotIn("only a reboot restores it", text)  # G-124: the revert word 0x39 equals the cold baseline
        self.assertIn("0x00000039", text)
        self.run_revert_plan(s, report, [0])
        self.assertEqual(s.sim.vf(0).to_u32(), 0x00000039)


class RevertWords(MeasuredCase):
    """G-122: the revert writes the word the hardware keeps (D-035), so the read-back matches."""

    def test_a_no_oc_point_baseline_with_an_offset_is_restored_exactly(self) -> None:
        s = self.session(config=SimConfig.measured(initial_vf={0: VfWord.from_u32(0xFEC00039)}))  # a previous tune left CPU 0 at -10 mV, ratio 57, no target
        report = s.apply(profile(p_core(0, 0)))
        self.assertTrue(report.ok, report.format_report())
        self.assertEqual(s.sim.vf(0).to_u32(), 0x00000039)  # G-124: offset 0 keeps no target either
        self.assertEqual([(r.cmd, r.data) for r in report.revert_plan][0], (0x11, 0xFEC00039))  # not 0xFEC5B439 (the hardware drops that: a read-back trip)
        self.run_revert_plan(s, report, [0])  # every revert request verified (whole-word read-back)
        self.assertEqual(s.sim.vf(0).to_u32(), 0xFEC00039)


class ECurve(MeasuredCase):
    """G-122: a whole-curve offset on E must not create an OC point (the OC ratio above 46 adds ``P2 = 7``)."""

    def point7_rc(self, s, cpu: int) -> int:
        return MailboxTransport(s.sim, sleep=s.fake.sleep, clock=s.fake.clock).transact(cpu, 0x10, 0, 7, 0).rc

    def test_e_whole_curve_minus_10_mv_adds_no_oc_point_and_back(self) -> None:
        s = self.session(config=SimConfig.measured(per_point=True))
        self.assertEqual(self.point7_rc(s, 12), 0x11)  # E has no P2 = 7 at stock
        report = s.apply(profile(e_core(4, offset_mv=-10)))  # E-Core 4 = cluster 1 = CPUs 12..15
        self.assertTrue(report.ok, report.format_report())
        for cpu in range(12, 16):
            word = s.sim.vf(cpu)
            self.assertEqual((word.ratio, word.v_target_raw, word.offset_raw), (46, 0, -10), cpu)  # ratio <= 46: no OC point, target 0 (D-035)
            self.assertEqual(self.point7_rc(s, cpu), 0x11, cpu)  # no OC point appeared
        self.assertEqual([(c, cmd) for c, cmd, _ in s.write_requests()], [(c, 0x11) for c in range(12, 16)])  # G-122: the 0x11 alone: no 0x1D = 46 (would likely cap E turbo at 46), no 0x27
        self.assertEqual(s.sim.favored_word(12), SimMsrPort(SimConfig.measured()).favored_word(12))  # 0x1C[7:0] still 48
        back = s.apply(profile(e_core(4, offset_mv=0)))
        self.assertTrue(back.ok, back.format_report())
        self.assertEqual({s.sim.vf(c).to_u32() for c in range(12, 16)}, {0x0000002E})  # G-124: offset 0 keeps no target either; ratio 46: still no OC point
        self.assertEqual(self.point7_rc(s, 12), 0x11)


    def test_the_default_step_limits_pass_the_cold_e_word_and_stop_a_real_raise(self) -> None:
        """G-122: max_ratio_step 1 (the shipped default): a cold E word at ratio 46 is no ratio step; an OC ratio two bins above the reference is still refused."""
        s = self.session(config=SimConfig.measured(per_point=True), policy=policy_with(max_ops=400, dwell=0.0, boot_ops=2000, **DEFAULT_STEPS))
        report = s.apply(profile(e_core(4, offset_mv=-10)))
        self.assertTrue(report.ok, report.format_report())
        self.assertEqual([(c, cmd) for c, cmd, _ in s.write_requests()], [(c, 0x11) for c in range(12, 16)])  # steps = [0x11 only]
        self.assertEqual(self.point7_rc(s, 12), 0x11)  # no OC point
        with self.assertRaises((NeedsRamp, PlanRefused)):
            s.apply(profile(e_core(0, ratio=50)))  # 48 -> 50: beyond max_ratio_step 1 (and above the policy ratio ceiling)
        self.assertEqual(len([r for r in s.write_requests() if r[1] == 0x11]), 4)


class Cpu1(MeasuredCase):
    def test_minus_5_mv_fills_ratio_from_0x1c_and_v_target_from_0x09(self) -> None:
        s = self.session()
        report = s.apply(profile(p_core(1, -5)))
        self.assertTrue(report.ok, report.format_report())
        self.assertEqual([r.step.cmd for r in report.steps], [0x14, 0x10, 0x1C, 0x09, 0x26, 0x11, 0x1D, 0x27])  # the task's step list
        self.assertEqual([(r.step.cmd, r.step.p1) for r in report.steps if r.step.cmd == 0x09], [(0x09, 0x11)])
        self.assertEqual(self.writes(s), [(1, 0x11, 0xFF600037), (1, 0x1D, 0x37), (1, 0x27, 0x3)])  # S-04d
        word = s.sim.vf(1)
        self.assertEqual((word.ratio, word.v_target_raw, word.static, word.offset_raw), (0x37, 0, False, -5))
        self.assertEqual(report.steps[1].after.to_u32(), 0)  # the baseline read: all zero
        self.assertEqual(s.sim.vf(0).to_u32(), 0x39)  # CPU 0 untouched
        self.assertEqual(s.sim.vf(2).to_u32(), 0)  # so is every other core

    def test_revert_is_xtus_0x00053837(self) -> None:
        s = self.session()
        s.apply(profile(p_core(1, -5)))
        s.sim.clear_log()
        report = s.apply(profile(p_core(1, 0)))
        self.assertTrue(report.ok, report.format_report())
        self.assertEqual(self.writes(s), [(1, 0x11, 0x00000037), (1, 0x1D, 0x37), (1, 0x27, 0x3)])  # G-124: XTU's 0x00053837 without the target the hardware drops
        self.assertEqual(s.sim.vf(1).to_u32(), 0x00000037)

    def test_a_plan_that_would_write_the_zero_baseline_back_does_not_exist(self) -> None:
        s = self.session()
        report = s.apply(profile(p_core(1, -5)))
        self.assertEqual(report.revert_plan[0].data, 0x00000037)  # never the baseline 0x0 (the zero-field sim would answer InvalidParam)
        sim = SimMsrPort(SimConfig.measured(reject_zero_fields=True))
        transport = MailboxTransport(sim, sleep=s.fake.sleep, clock=s.fake.clock)
        self.assertEqual(transport.transact(1, 0x11, 0, 0, 0x0).rc, 0x10)  # the baseline word written back as-is: InvalidParam (SIM-ONLY rule)


class ECores(MeasuredCase):
    ALL_16 = (e_core(0, offset_mv=-5), e_core(4, offset_mv=-5), e_core(8, offset_mv=-5), e_core(12, offset_mv=-5))
    ALL_16_REVERT = (e_core(0, offset_mv=0), e_core(4, offset_mv=0), e_core(8, offset_mv=0), e_core(12, offset_mv=0))

    def test_all_16_e_cores_minus_5_mv_and_back(self) -> None:
        for wide in (False, True):
            with self.subTest(e_cluster_wide=wide):
                tmp = self.tmp / f"w{wide}"
                tmp.mkdir()
                s = make_session(
                    tmp,
                    config=SimConfig.measured(e_cluster_wide=wide),
                    policy=policy_with(max_ops=400, dwell=0.0, boot_ops=2000),
                )
                report = s.apply(profile(*self.ALL_16))
                self.assertTrue(report.ok, report.format_report())
                self.assertEqual(report.operations, 1 + 16 * 5)  # G-122: reads 0x10 0x1C 0x09 0x26 + the 0x11 per CPU; no 0x1D / 0x27 without an OC ratio
                writes = s.write_requests()
                self.assertEqual(len(writes), 16)
                for cpu in range(8, 24):
                    want = 0xFF60002E  # G-122: ratio 0x2E = 46 (top fused E ratio: no OC point), V target 0 (the hardware keeps none), offset -5
                    self.assertEqual(s.sim.vf(cpu).to_u32(), want, cpu)
                    self.assertIn((cpu, 0x11, want), writes)
                for cpu in range(8):
                    self.assertEqual(s.sim.vf(cpu).to_u32(), 0x39 if cpu == 0 else 0, cpu)  # P cores untouched
                self.assertEqual(report.zero_baseline_cpus, tuple(range(8, 24)))
                s.sim.clear_log()
                back = s.apply(profile(*self.ALL_16_REVERT))
                self.assertTrue(back.ok, back.format_report())
                for cpu in range(8, 24):
                    self.assertEqual(s.sim.vf(cpu).to_u32(), 0x0000002E, cpu)  # S-07 shape at ratio 46 (G-122); G-124: target 0 at offset 0 too

    def test_one_cluster_is_written_as_all_four_cores_never_one(self) -> None:
        s = self.session()
        report = s.apply(profile(e_core(4, offset_mv=-5)))  # E-Core 4 = Linux CPU 12, cluster 1
        self.assertTrue(report.ok, report.format_report())
        self.assertEqual([c for c, cmd, _ in s.write_requests() if cmd == 0x11], [12, 13, 14, 15])
        self.assertTrue(any("cluster" in w and "UNVERIFIED" in w for w in report.warnings))  # C-4 stays open


class RefusalsOnTheMeasuredMachine(MeasuredCase):
    def refused(self, prof, reads=XTU_READS, **kwargs):
        s = self.session(**kwargs)
        with self.assertRaises(PlanRefused) as ctx:
            s.apply(prof)
        self.assertEqual([cmd for _, cmd, _ in s.requests()], reads)  # one core: only the reads were sent ([]: refused before the first)
        self.assertEqual(s.write_requests(), [])  # nothing written
        return ctx.exception

    def test_overvolt_above_the_fused_point(self) -> None:
        # raw 1536 > fused 0x538 = 1336, the no-raise ceiling of the shipped policy; an OC-point voltage also reads 0x28
        exc = self.refused(  # G-124: only an OC point carries a V target: P1 sits at 58 (> 57)
            profile('kind = "P"\nindex = 1\noc_voltage_mv = 1500'), reads=[*XTU_READS, 0x28], config=SimConfig.measured(initial_vf={1: VfWord(58, 0x538, False, 0)})
        )
        self.assertIn("v_target_raw", exc.refusals[0].detail)
        self.assertIn("no-raise value", exc.refusals[0].detail)

    def test_ratio_above_the_0x1c_low_byte(self) -> None:
        exc = self.refused(profile('kind = "P"\nindex = 1\nratio = 56'))  # CPU 1: 0x1C[7:0] = 55
        self.assertIn("ratio 56 above 55", exc.refusals[0].detail)
        s = self.session()
        self.assertTrue(s.apply(profile('kind = "P"\nindex = 1\nratio = 55')).ok)  # at the reference: allowed

    def test_the_d015_floor_end_to_end(self) -> None:
        exc = self.refused(profile(p_core(1, -51)), reads=[])  # P: -50 mV is the runtime floor; refused before the first read (D-016)
        self.assertIn("D-015", exc.refusals[0].detail)
        self.assertIsNone(exc.report)
        s = self.session()
        self.assertTrue(s.apply(profile(p_core(1, -50))).ok)
        self.assertEqual(s.sim.vf(1).offset_raw, -51)
        tmp = self.tmp / "e"
        tmp.mkdir()
        s = make_session(tmp, config=SimConfig.measured(), policy=policy_with(max_ops=400, dwell=0.0, boot_ops=2000))
        self.assertTrue(s.apply(profile(e_core(0, offset_mv=-100))).ok)  # E: -100 mV allowed
        self.assertEqual(s.sim.vf(8).offset_raw, -102)
        tmp = self.tmp / "e2"
        tmp.mkdir()
        s = make_session(tmp, config=SimConfig.measured(), policy=policy_with(max_ops=400, dwell=0.0, boot_ops=2000))
        with self.assertRaises(PlanRefused):
            s.apply(profile(e_core(0, offset_mv=-101)))

    def test_the_stage_of_a_refusal_is_after_the_reads_and_before_every_write(self) -> None:
        s = self.session()
        with self.assertRaises(PlanRefused) as ctx:
            s.apply(profile('kind = "P"\nindex = 0\nratio = 58'))  # CPU 0: 0x1C[7:0] = 57, so 58 is judged on the words read
        self.assertEqual([r.step.kind for r in ctx.exception.report.steps][-1], StepKind.READ_GRANULAR)
        self.assertEqual(ctx.exception.report.stop_kind, StopKind.NONE)
        self.assertEqual(s.sim.vf(0).to_u32(), 0x39)


if __name__ == "__main__":
    unittest.main()
