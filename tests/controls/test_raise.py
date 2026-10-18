"""T-022 end-to-end tests: raising ratio / adaptive V target / offset by a reviewed policy (D-016), through ``apply_profile``, the REAL guard
and ``SimConfig.measured()`` (``reject_zero_fields=True`` throughout, via ``make_session``).

Measured machine (``[MEASURED win 2026-10-01]``, win-xtu-live-trace.md S-03d/S-06): E cluster 0 = CPUs 8-11, ratio 48 (``0x1C[7:0]``), fused V
target 0x4A0 = 1184 raw (1.156 V), ``0x28`` = 0x56D; P core 1 = CPU 1, ratio 55, fused V target 0x538. **Whether the hardware accepts a raise
is ``[UNVERIFIED]``**: the simulator's pcode model (``oc_cap_ratio``, ``max_voltage_cpu``, both configurable) is its own SIM-ONLY rule set; these
tests prove what arltuner sends, in which order, and what it refuses -- not what silicon does.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from arltuner.controls import CoreReadings, NeedsHigherLimit, NeedsRamp, PlanRefused, StepKind, StopKind, build_plan, format_plan, resolve_targets
from arltuner.guard import ENV_FLAG, AuditLog, Confirmation, Request, WriteGuard
from arltuner.mailbox import MSR_OC_MAILBOX, VfWord
from arltuner.msr import MailboxTransport, SimConfig
from arltuner.msr import measured_baseline as measured
from arltuner.profiles import load_profile
from arltuner.topology import arl_s_285k

from ._support import e_core, make_session, policy_with, profile

#: D-049: the policy carries no value limits; a raise is judged against the CPU's own limits (cmd 0x01 ratio, cmd 0x28 voltage) only
RAISE: dict = {}
E_CPUS = (8, 9, 10, 11)
E_V = 0x4A0  # 1184 raw: the fused V target of E cluster 0
V_UP = E_V + 20  # +20 raw; profile value 1176 mV -> round(1.176 * 1024) = 1204


def word(ratio: int, v_raw: int, offset_raw: int = 0) -> int:
    return VfWord(ratio, v_raw, False, offset_raw).to_u32()


class RaiseCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self._n = 0

    def session(self, *, config=None, **policy_changes):
        self._n += 1
        sub = self.tmp / f"s{self._n}"
        sub.mkdir()
        policy = policy_with(max_ops=400, dwell=0.0, boot_ops=2000, **policy_changes)
        return make_session(sub, config=SimConfig.measured() if config is None else config, policy=policy)

    @staticmethod
    def log(s, cpu: int) -> list[tuple[int, int]]:
        """(cmd, data) of every request the simulator latched for ``cpu`` (read-backs included), in order."""
        return [(c, d) for cpu_, c, d in s.requests() if cpu_ == cpu]


class RaiseWithinTheCpuLimits(RaiseCase):
    def test_e_cluster_48_to_49_at_plus_20_mv_is_sent_voltage_first_and_read_back(self) -> None:
        s = self.session(**RAISE)
        report = s.apply(profile(e_core(0, ratio=49, oc_voltage_mv=1176)))
        self.assertTrue(report.ok, report.format_report())
        for cpu in E_CPUS:
            self.assertEqual(
                self.log(s, cpu),
                [
                    *([(0x14, 0)] if cpu == 8 else []),  # the package-wide 0x14 read goes to the first target CPU
                    (0x10, 0), (0x1C, 0), (0x09, 0), (0x26, 0), (0x28, 0),  # the reads, 0x28 (hardware maximum) because an OC-point voltage is set
                    (0x11, word(48, V_UP)), (0x10, 0),  # write 1: the NEW V target with the OLD ratio, read back
                    (0x11, word(49, V_UP)), (0x10, 0),  # write 2: the full word, read back
                    (0x1D, 49), (0x1C, 0),  # 0x1D follows the RAISED ratio
                    (0x27, 3), (0x26, 0),  # 0x27 = the 0x26 word read
                ],
                cpu,
            )
            self.assertEqual(s.sim.vf(cpu), VfWord(49, V_UP, False, 0), cpu)
            self.assertEqual(s.sim.favored_word(cpu) & 0xFF, 49, cpu)
        self.assertEqual(s.sim.vf(12).to_u32(), 0x0)  # the next cluster was not touched
        writes = [r for r in report.steps if r.step.kind is StepKind.WRITE_VF]
        self.assertEqual(len(writes), 8)  # two words per CPU
        self.assertTrue(all(r.verified for r in report.steps if r.step.kind.is_write))  # every write read back equal
        self.assertEqual(report.operations, 1 + 4 * (5 + 4))  # 0x14, then per CPU 5 reads + 4 writes
        self.assertFalse(report.tripped)

    def test_the_two_words_are_distinct_steps_with_stage_labels_in_the_plan(self) -> None:
        s = self.session(**RAISE)
        report = s.apply(profile(e_core(0, ratio=49, oc_voltage_mv=1176)))
        stages = [r.step.stage for r in report.steps if r.step.kind is StepKind.WRITE_VF and r.step.cpu == 8]
        self.assertEqual(stages, ["1/2 voltage first", "2/2 full word"])
        self.assertIn("[1/2 voltage first]", report.plan.steps[0].purpose + "".join(st.purpose for st in report.plan.steps))

    def test_a_ratio_only_raise_is_one_word_and_reads_no_0x28(self) -> None:
        s = self.session(**RAISE)
        report = s.apply(profile('kind = "P"\nindex = 1\nratio = 56'))  # P core 1: 55 -> 56, ceiling 58
        self.assertTrue(report.ok, report.format_report())
        self.assertEqual(
            self.log(s, 1),
            [(0x14, 0), (0x10, 0), (0x1C, 0), (0x09, 0), (0x26, 0), (0x11, word(56, 0)), (0x10, 0), (0x1D, 56), (0x1C, 0), (0x27, 3), (0x26, 0)],
        )
        self.assertEqual(s.sim.vf(1), VfWord(56, 0, False, 0))  # G-124: ratio 56 <= 57 is no OC point: the word carries no V target

    def test_a_positive_offset_of_25_mv(self) -> None:
        s = self.session(**RAISE)
        self.assertTrue(s.apply(profile(e_core(0, offset_mv=25))).ok)
        self.assertEqual(s.sim.vf(8).offset_raw, 26)  # +25 mV = raw 26


class RaiseNeedsTheCpuLimits(RaiseCase):
    """D-049: no operator ceiling decides a raise; the CPU's own limits (0x01 ratio, 0x28 voltage) do, and an unread limit refuses a raise."""

    def test_ratio_raise_and_v_target_raise_are_allowed_when_the_cpu_limits_are_bound(self) -> None:
        s = self.session()
        self.assertTrue(s.apply(profile(e_core(0, ratio=49))).ok)
        self.assertEqual(s.sim.vf(8).ratio, 49)
        s = self.session()
        self.assertTrue(s.apply(profile(e_core(0, oc_voltage_mv=1176))).ok)
        self.assertEqual(s.sim.vf(8).v_target_raw, V_UP)

    def test_a_positive_offset_is_allowed_and_reads_0x28(self) -> None:
        s = self.session()
        self.assertTrue(s.apply(profile(e_core(0, offset_mv=5))).ok)
        self.assertEqual(s.sim.vf(8).offset_raw, 5)
        self.assertIn(0x28, [c for cpu, c, _ in s.requests() if cpu == 8])  # a positive whole-curve offset is judged against the 0x28 limit

    def test_lowering_and_undervolting_still_work(self) -> None:
        s = self.session()
        self.assertTrue(s.apply(profile(e_core(0, ratio=47, offset_mv=-20))).ok)  # 48 -> 47, offset -20 mV
        self.assertEqual(s.sim.vf(8), VfWord(47, E_V, False, -20))
        # cluster 1: the fused V target 0x48C = 1164 raw = 1136.7 mV; 1137 mV converts to raw 1164 again: an unchanged V target is no raise
        self.assertTrue(s.apply(profile(e_core(4, oc_voltage_mv=1137))).ok)
        self.assertEqual(s.sim.vf(12).v_target_raw, 1164)

    def test_an_already_raised_machine_is_not_forced_down(self) -> None:
        config = SimConfig.measured(initial_vf={c: VfWord(49, V_UP, False, 0) for c in E_CPUS}, favored_word={c: 0x2E31 for c in E_CPUS})
        s = self.session(config=config)
        self.assertTrue(s.apply(profile(e_core(0, ratio=49, offset_mv=-10))).ok)
        self.assertTrue(self.session(config=config).apply(profile(e_core(0, ratio=50))).ok)  # one bin higher is a raise: no step limit, the CPU limit (120) allows it

    def test_a_raise_with_the_ratio_limit_unread_is_refused_by_the_guard(self) -> None:
        # the plan is built with the limit bound (as the helper does after tune.read); a guard that does not know it refuses the raise, fail closed
        s = self.session(hw_ratio_max={"P": None, "E": None})
        with self.assertRaises(PlanRefused) as ctx:
            s.apply(profile(e_core(0, ratio=49)), policy=self.session().policy)
        self.assertIn("CPU_LIMIT_UNREAD", ctx.exception.refusals[0].detail)
        self.assertEqual(s.write_requests(), [])

    def test_a_lowering_with_the_limits_unread_passes(self) -> None:
        s = self.session(hw_ratio_max={"P": None, "E": None})
        self.assertTrue(s.apply(profile(e_core(0, ratio=47, offset_mv=-20))).ok)


class AboveTheBounds(RaiseCase):
    def test_a_value_above_the_cpu_limit_is_refused_before_any_write(self) -> None:
        cases = {
            "ratio 51 above the CPU limit E = 50": profile(e_core(0, ratio=51)),
            "V target 1251 mV above the CPU limit E = 1250": profile(e_core(0, oc_voltage_mv=1251)),
        }
        for name, prof in cases.items():
            with self.subTest(name):
                s = self.session(hw_ratio_max={"E": 50, "P": 58}, hw_v_target_max_mv={"E": 1250, "P": 1625})
                with self.assertRaises(PlanRefused) as ctx:
                    s.apply(prof)
                self.assertEqual(ctx.exception.refusals[0].reason, "CHECK_FAILED")
                self.assertEqual(s.write_requests(), [])
                self.assertEqual(s.audit()[-1:] and [r for r in s.audit() if r.get("cmd") in (0x11, "0x11")], [])  # no 0x11 was ever attempted

    def test_the_hardware_maximum_of_0x28_bounds_a_v_target_raise(self) -> None:
        # this machine's 0x28 answers 0x4B0: hardware maximum 0x4AF = 1199 raw
        config = SimConfig.measured(max_voltage_cpu={c: 0x4B0 for c in E_CPUS})
        s = self.session(config=config)
        with self.assertRaises(PlanRefused) as ctx:
            s.apply(profile(e_core(0, oc_voltage_mv=1200)))  # raw 1229 > 1199
        self.assertIn("CPU limit", ctx.exception.refusals[0].detail)
        self.assertEqual(s.write_requests(), [])
        self.assertTrue(self.session(config=config).apply(profile(e_core(0, oc_voltage_mv=1165))).ok)

    def test_a_raise_needs_a_session_that_can_afford_the_extra_read_and_write(self) -> None:
        # worst case before any read: the 0x14 read + 4 CPUs x (5 reads + 4 writes) + the 0x15 write = 38 operations, + 17 read-backs = 55
        s = self.session(max_operations_per_session=54, **RAISE)
        with self.assertRaises(NeedsHigherLimit) as ctx:
            s.apply(profile(e_core(0, ratio=49, oc_voltage_mv=1176)))
        self.assertEqual((ctx.exception.required, ctx.exception.allowed), (55, 54))
        self.assertEqual(s.sim.requests, [])
        s = self.session(max_operations_per_session=55, **RAISE)
        self.assertTrue(s.apply(profile(e_core(0, ratio=49, oc_voltage_mv=1176))).ok)

    def test_the_pcode_model_may_still_refuse_what_the_cpu_limit_allows(self) -> None:
        # SimConfig.oc_cap_ratio is the simulator's pcode model, configurable: here the E cap is 48, the bound CPU limit (120) allows 49
        config = SimConfig.measured(oc_cap_ratio={"P": 62, "E": 48})
        s = self.session(config=config, **RAISE)
        report = s.apply(profile(e_core(0, ratio=49)))
        self.assertEqual(report.stop_kind, StopKind.RC)
        last = report.steps[-1]
        self.assertEqual((last.step.cmd, last.rc, last.rc_name), (0x11, 3, "RatioExceedsLimits"))
        self.assertEqual(s.write_requests(), [(8, 0x11, word(49, E_V))])  # stopped at the first refusal, nothing further sent
        self.assertEqual(s.sim.vf(8).to_u32(), 0x0)  # and nothing changed
        self.assertFalse(report.tripped)


class OneRound(RaiseCase):
    """D-049: no step limits. A big raise is ONE plan, one apply: no NeedsRamp, no next-step profile."""

    GOAL = e_core(0, ratio=50, oc_voltage_mv=1200)  # 48 -> 50 and 1184 -> 1229 raw

    def test_a_big_raise_is_applied_in_one_call(self) -> None:
        s = self.session()
        report = s.apply(profile(self.GOAL))
        self.assertTrue(report.ok, report.format_report())
        self.assertEqual(s.sim.vf(8), VfWord(50, 1229, False, 0))
        sent = [VfWord.from_u32(d) for cpu, c, d in s.requests() if cpu == 8 and c == 0x11]
        self.assertEqual(len(sent), 2)  # voltage first, then the full word
        self.assertFalse(sent[1].ratio > sent[0].ratio and sent[1].v_target_raw > sent[0].v_target_raw and sent[0] != VfWord(48, 1229, False, 0))

    def test_the_voltage_goes_first_when_both_rise(self) -> None:
        s = self.session()
        s.apply(profile(self.GOAL))
        sent = [VfWord.from_u32(d) for cpu, c, d in s.requests() if cpu == 8 and c == 0x11]
        self.assertEqual(sent, [VfWord(48, 1229, False, 0), VfWord(50, 1229, False, 0)])

    def test_the_dry_run_plan_is_allowed_and_has_no_next_step(self) -> None:
        policy = policy_with(max_ops=400, dwell=0.0, boot_ops=2000, **RAISE)
        readings = {
            cpu: CoreReadings(
                vf=VfWord.from_u32(measured.VF_DOMAIN0[cpu]),
                favored=measured.FAVORED_WORD[cpu],
                fused=measured.FUSED_POINT[cpu],
                granular=3,
                vmax=measured.MAX_VOLTAGE_RAW[cpu],
            )
            for cpu in E_CPUS
        }
        plan = build_plan(profile(self.GOAL), arl_s_285k(), readings, per_core_enabled=True, policy=policy)
        self.assertTrue(plan.allowed)
        self.assertFalse(plan.needs_ramp)
        self.assertIsNone(plan.next_profile_text)
        text = format_plan(plan)
        self.assertNotIn("NEXT ALLOWED STEP", text)
        self.assertIn("RAISES", " ".join(plan.warnings))  # the [UNVERIFIED] raise caveat
        plan.require_allowed()

    def test_a_goal_above_the_cpu_limit_is_a_refusal(self) -> None:
        s = self.session(hw_ratio_max={"E": 50, "P": 58})
        with self.assertRaises(PlanRefused):
            s.apply(profile(e_core(0, ratio=51, oc_voltage_mv=1200)))

    def test_the_whole_profile_is_one_plan(self) -> None:
        s = self.session()
        report = s.apply(profile(e_core(0, ratio=50), e_core(4, offset_mv=-10)))
        self.assertTrue(report.ok, report.format_report())
        self.assertEqual(s.sim.vf(8).ratio, 50)
        self.assertEqual(s.sim.vf(12).offset_raw, VfWord(0, 0, False, -10).offset_raw)


class ClusterScope(RaiseCase):
    def test_an_e_entry_is_always_the_whole_cluster_with_one_word(self) -> None:
        # [UNVERIFIED] E-core write scope (C-4): a single-CPU E write is never planned -- every entry expands to the four CPUs
        for index in (0, 1, 2, 3):
            targets = resolve_targets(profile(e_core(index, ratio=49)), arl_s_285k())
            self.assertEqual([t.cpu for t in targets], [8, 9, 10, 11], index)
        s = self.session(**RAISE)
        s.apply(profile(e_core(2, ratio=49)))
        by_cpu = {cpu: [d for c, d in self.log(s, cpu) if c == 0x11] for cpu in E_CPUS}
        self.assertEqual(len({tuple(v) for v in by_cpu.values()}), 1)  # the same words on every CPU of the cluster
        self.assertTrue(all(len(v) == 1 for v in by_cpu.values()))  # a ratio-only raise is one word per CPU

    def test_the_cpu_limits_are_per_kind(self) -> None:
        s = self.session(hw_ratio_max={"E": 49, "P": 58})
        with self.assertRaises(PlanRefused):
            s.apply(profile(e_core(0, ratio=50)))  # above the E limit
        s = self.session(hw_ratio_max={"E": 49, "P": 58})
        self.assertTrue(s.apply(profile('kind = "P"\nindex = 1\nratio = 56')).ok)  # the P limit is higher


class RevertOfARaise(RaiseCase):
    def raised(self):
        s = self.session(**RAISE)
        report = s.apply(profile(e_core(0, ratio=49, oc_voltage_mv=1176)))
        self.assertTrue(report.ok, report.format_report())
        return s, report

    def test_the_revert_plan_is_frequency_first_and_xtu_shaped(self) -> None:
        s, report = self.raised()
        for_cpu8 = [(r.cmd, r.data) for r in report.revert_plan if r.cpu == 8]
        self.assertEqual(
            for_cpu8,
            [(0x11, word(48, V_UP)), (0x11, word(48, E_V)), (0x1D, 48), (0x27, 3)],  # ratio back first, then the V target; never 0x0
        )
        self.assertEqual([r.cpu for r in report.revert_plan if r.cmd == 0x11], [11, 11, 10, 10, 9, 9, 8, 8])  # newest CPU first, two words each
        for r in report.revert_plan:
            if r.cmd == 0x11:
                w = VfWord.from_u32(r.data)
                self.assertTrue(w.ratio > 0 and w.v_target_raw > 0 and not w.static)  # never a word with a zero field
        text = report.format_report()
        self.assertIn("a reboot restores the BIOS-programmed state", text)
        self.assertIn("after a raise the ratio first, then the voltage", text)

    def test_the_revert_requests_run_in_a_new_session_and_restore_the_xtu_shaped_word(self) -> None:
        s, report = self.raised()
        policy = policy_with(max_ops=400, dwell=0.0, boot_ops=2000, **RAISE)
        g = WriteGuard(
            MailboxTransport(s.sim, sleep=s.fake.sleep, clock=s.fake.clock),
            policy,
            AuditLog(self.tmp / "revert-audit.jsonl", fsync=False),
            session_token="rv",
            boot_id_fn=lambda: "test-boot",
            env={ENV_FLAG: "1"},
            lock_path=self.tmp / "revert.lock",
            clock=s.fake.clock,
        )
        confirm = Confirmation("rv", "tester")
        for cpu in E_CPUS:  # the new session's own baseline reads see the RAISED state
            for cmd, p1 in ((0x10, 0), (0x1C, 0), (0x09, 0x11), (0x26, 0)):
                g.execute(Request(cpu, cmd, p1, 0, 0, "baseline"), confirm)
        for req in report.revert_plan:
            self.assertIs(g.execute(req, confirm).verified, True, req)
        for cpu in E_CPUS:
            self.assertEqual(s.sim.vf(cpu), VfWord(48, E_V, False, 0), cpu)  # XTU-shaped: ratio 48, fused V target, offset 0 (not 0x0)
            self.assertEqual(s.sim.favored_word(cpu) & 0xFF, 48, cpu)
        self.assertNotEqual(s.sim.vf(8).to_u32(), 0)
        self.assertFalse(g.tripped)


if __name__ == "__main__":
    unittest.main()
