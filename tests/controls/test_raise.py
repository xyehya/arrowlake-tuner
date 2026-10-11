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

from ._support import DEFAULT_STEPS, e_core, make_session, policy_with, profile

#: a reviewed policy that allows a raise: E up to ratio 50 / 1.250 V, P up to ratio 58 (V target blank = no raise), shipped step limits
RAISE = dict(ratio_max={"E": 50, "P": 58}, v_target_max_mv={"E": 1250})
E_CPUS = (8, 9, 10, 11)
E_V = 0x4A0  # 1184 raw: the fused V target of E cluster 0
V_UP = E_V + 20  # +20 raw = the 20 mV step limit; profile value 1176 mV -> round(1.176 * 1024) = 1204


def word(ratio: int, v_raw: int, offset_raw: int = 0) -> int:
    return VfWord(ratio, v_raw, False, offset_raw).to_u32()


class RaiseCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self._n = 0

    def session(self, *, steps=DEFAULT_STEPS, config=None, **policy_changes):
        self._n += 1
        sub = self.tmp / f"s{self._n}"
        sub.mkdir()
        policy = policy_with(max_ops=400, dwell=0.0, boot_ops=2000, **{**steps, **policy_changes})
        return make_session(sub, config=SimConfig.measured() if config is None else config, policy=policy)

    @staticmethod
    def log(s, cpu: int) -> list[tuple[int, int]]:
        """(cmd, data) of every request the simulator latched for ``cpu`` (read-backs included), in order."""
        return [(c, d) for cpu_, c, d in s.requests() if cpu_ == cpu]


class RaiseWithinTheCeilings(RaiseCase):
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

    def test_a_positive_offset_up_to_the_ceiling(self) -> None:
        s = self.session(offset_ceiling_mv=25, **RAISE)
        self.assertTrue(s.apply(profile(e_core(0, offset_mv=25))).ok)
        self.assertEqual(s.sim.vf(8).offset_raw, 26)  # +25 mV = raw 26, exactly the ceiling and the step limit


class TheShippedPolicyRefusesEveryRaise(RaiseCase):
    def refused_after_reads(self, prof, fragment: str, reads: list[int]):
        s = self.session(**{})  # the shipped bounds: no ratio / V ceilings (no-raise), ceiling 0, default steps
        with self.assertRaises(PlanRefused) as ctx:
            s.apply(prof)
        self.assertIn(fragment, ctx.exception.refusals[0].detail)
        self.assertEqual(s.write_requests(), [])
        self.assertEqual([c for cpu, c, _ in s.requests() if cpu == 8], reads)
        return ctx.exception

    def test_ratio_raise(self) -> None:
        self.refused_after_reads(profile(e_core(0, ratio=49)), "no-raise value", [0x14, 0x10, 0x1C, 0x09, 0x26])

    def test_v_target_raise(self) -> None:
        self.refused_after_reads(profile(e_core(0, oc_voltage_mv=1176)), "no-raise value", [0x14, 0x10, 0x1C, 0x09, 0x26, 0x28])

    def test_positive_offset_is_refused_before_anything_is_sent(self) -> None:
        s = self.session()
        with self.assertRaises(PlanRefused) as ctx:
            s.apply(profile(e_core(0, offset_mv=5)))
        self.assertIn("offset_ceiling_mv", ctx.exception.refusals[0].detail)
        self.assertEqual(s.sim.requests, [])

    def test_lowering_and_undervolting_still_work(self) -> None:
        s = self.session()
        self.assertTrue(s.apply(profile(e_core(0, ratio=47, offset_mv=-20))).ok)  # 48 -> 47, offset -20 mV
        self.assertEqual(s.sim.vf(8), VfWord(47, E_V, False, -20))
        # cluster 1: the fused V target 0x48C = 1164 raw = 1136.7 mV; 1137 mV converts to raw 1164 again: an unchanged V target is no raise
        self.assertTrue(s.apply(profile(e_core(4, oc_voltage_mv=1137))).ok)
        self.assertEqual(s.sim.vf(12).v_target_raw, 1164)

    def test_an_already_raised_machine_is_not_forced_down_by_the_default_policy(self) -> None:
        # the default no-raise ceiling is the CURRENT value: a core left at ratio 49 by an earlier policy may stay there
        config = SimConfig.measured(initial_vf={c: VfWord(49, V_UP, False, 0) for c in E_CPUS}, favored_word={c: 0x2E31 for c in E_CPUS})
        s = self.session(config=config)
        self.assertTrue(s.apply(profile(e_core(0, ratio=49, offset_mv=-10))).ok)
        with self.assertRaises(PlanRefused):
            self.session(config=config).apply(profile(e_core(0, ratio=50)))  # but one bin higher is a raise


class AboveTheBounds(RaiseCase):
    def test_a_value_above_an_explicit_ceiling_is_refused_before_the_first_read(self) -> None:
        cases = {
            "ratio 51 above ratio_max E = 50": profile(e_core(0, ratio=51)),
            "V target 1251 mV above v_target_max_mv E = 1250": profile(e_core(0, oc_voltage_mv=1251)),
            "offset above the ceiling": profile(e_core(0, offset_mv=1)),
        }
        for name, prof in cases.items():
            with self.subTest(name):
                s = self.session(**RAISE)
                with self.assertRaises(PlanRefused) as ctx:
                    s.apply(prof)
                self.assertEqual(ctx.exception.refusals[0].reason, "CHECK_FAILED")
                self.assertEqual(s.sim.requests, [])  # nothing at all
                self.assertEqual(s.audit(), [])

    def test_the_hardware_maximum_of_0x28_bounds_a_policy_that_allows_more(self) -> None:
        # the policy allows 1.250 V (raw 1280) but this machine's 0x28 answers 0x4B0: hardware maximum 0x4AF = 1199 raw
        config = SimConfig.measured(max_voltage_cpu={c: 0x4B0 for c in E_CPUS})
        s = self.session(config=config, steps=dict(max_ratio_step=8, max_v_step_mv=200, max_offset_step_mv=200), **RAISE)
        with self.assertRaises(PlanRefused) as ctx:
            s.apply(profile(e_core(0, oc_voltage_mv=1200)))  # raw 1229 > 1199
        self.assertIn("hardware maximum", ctx.exception.refusals[0].detail)
        self.assertEqual(s.write_requests(), [])
        self.assertTrue(self.session(config=config, steps=dict(max_ratio_step=8, max_v_step_mv=200, max_offset_step_mv=200), **RAISE).apply(profile(e_core(0, oc_voltage_mv=1165))).ok)

    def test_a_raise_needs_a_session_that_can_afford_the_extra_read_and_write(self) -> None:
        # worst case before any read: the 0x14 read + 4 CPUs x (5 reads + 4 writes) + the 0x15 write = 38 operations, + 17 read-backs = 55
        s = self.session(max_operations_per_session=54, **RAISE)
        with self.assertRaises(NeedsHigherLimit) as ctx:
            s.apply(profile(e_core(0, ratio=49, oc_voltage_mv=1176)))
        self.assertEqual((ctx.exception.required, ctx.exception.allowed), (55, 54))
        self.assertEqual(s.sim.requests, [])
        s = self.session(max_operations_per_session=55, **RAISE)
        self.assertTrue(s.apply(profile(e_core(0, ratio=49, oc_voltage_mv=1176))).ok)

    def test_the_pcode_model_may_still_refuse_what_the_policy_allows(self) -> None:
        # SimConfig.oc_cap_ratio is the simulator's pcode model, configurable: here the E cap is 48, the policy allows 50
        config = SimConfig.measured(oc_cap_ratio={"P": 62, "E": 48})
        s = self.session(config=config, **RAISE)
        report = s.apply(profile(e_core(0, ratio=49)))
        self.assertEqual(report.stop_kind, StopKind.RC)
        last = report.steps[-1]
        self.assertEqual((last.step.cmd, last.rc, last.rc_name), (0x11, 3, "RatioExceedsLimits"))
        self.assertEqual(s.write_requests(), [(8, 0x11, word(49, E_V))])  # stopped at the first refusal, nothing further sent
        self.assertEqual(s.sim.vf(8).to_u32(), 0x0)  # and nothing changed
        self.assertFalse(report.tripped)


class Ramp(RaiseCase):
    GOAL = e_core(0, ratio=50, oc_voltage_mv=1200)  # 48 -> 50 and 1184 -> 1229 raw: more than one step

    def test_a_step_that_is_too_big_is_refused_with_the_exact_next_step(self) -> None:
        s = self.session(**RAISE)
        with self.assertRaises(NeedsRamp) as ctx:
            s.apply(profile(self.GOAL))
        self.assertEqual(
            ctx.exception.next_profile_text,
            '[meta]\nname = "t (next step)"\nplatform = "arl-s-285k"\n\n[[core]]\nkind = "E"\nindex = 0\nratio = 49\noc_voltage_mv = 1176\n',
        )
        self.assertEqual(s.write_requests(), [])  # nothing was written
        self.assertEqual(s.sim.vf(8).to_u32(), 0x0)
        self.assertIsNotNone(ctx.exception.report)  # the reads that preceded it
        self.assertIn("1176", str(ctx.exception))  # the message carries the profile
        self.assertEqual(load_profile(ctx.exception.next_profile_text).entries[0].oc_voltage_mv, 1176)  # it is a valid profile

    def test_walking_the_steps_converges_on_the_goal_and_every_step_is_within_the_limits(self) -> None:
        s = self.session(**RAISE)
        goal = profile(self.GOAL)
        states = []
        prof = goal
        for _ in range(6):
            try:
                report = s.apply(prof)
            except NeedsRamp as exc:
                prof = load_profile(exc.next_profile_text)
                continue
            self.assertTrue(report.ok, report.format_report())
            states.append((s.sim.vf(8).ratio, s.sim.vf(8).v_target_raw))
            if prof is goal:
                break
            prof = goal  # after the next step, ask for the goal again
        self.assertEqual(states[-1], (50, VfWord.v_to_raw(1.2)))
        # step 1: 49 / 1204; step 2: 50 / 1224; step 3: the goal 50 / 1229
        self.assertEqual(states, [(49, V_UP), (50, V_UP + 20), (50, 1229)])

    def test_each_walked_step_is_voltage_first_when_both_rise(self) -> None:
        s = self.session(**RAISE)
        goal = profile(self.GOAL)
        prof = goal
        for _ in range(6):
            try:
                s.apply(prof)
            except NeedsRamp as exc:
                prof = load_profile(exc.next_profile_text)
                continue
            if prof is goal:
                break
            prof = goal
        sent = [d for cpu, c, d in s.requests() if cpu == 8 and c == 0x11]
        words = [VfWord.from_u32(d) for d in sent]
        for before, after in zip(words, words[1:]):  # no word moved both fields up relative to the previous word of the same cpu
            self.assertFalse(after.ratio > before.ratio and after.v_target_raw > before.v_target_raw, (before, after))

    def test_the_dry_run_text_shows_the_refused_step_and_the_next_step(self) -> None:
        # what `plan` prints, built from the measured words (no session, nothing sent)
        policy = policy_with(max_ops=400, dwell=0.0, boot_ops=2000, **{**DEFAULT_STEPS, **RAISE})
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
        self.assertTrue(plan.needs_ramp)
        self.assertFalse(plan.allowed)
        text = format_plan(plan)
        self.assertIn("NEXT ALLOWED STEP", text)
        self.assertIn("ratio = 49", text)
        self.assertIn("oc_voltage_mv = 1176", text)
        self.assertIn("NOT ALLOWED (needs a smaller step", text)
        self.assertIn("REFUSED CHECK_FAILED: vf_bounded:", text)  # the requested word is shown with the guard's reason
        self.assertIn("RAISES", " ".join(plan.warnings))  # the [UNVERIFIED] raise caveat
        with self.assertRaises(NeedsRamp):
            plan.require_allowed()

    def test_a_ceiling_that_forbids_the_goal_is_a_refusal_not_a_ramp(self) -> None:
        # ratio 51 is above ratio_max 50: no sequence of small steps reaches it, so no "next step" is offered
        s = self.session(**RAISE)
        with self.assertRaises(PlanRefused):
            s.apply(profile(e_core(0, ratio=51, oc_voltage_mv=1200)))

    def test_one_cluster_that_needs_a_ramp_ramps_the_whole_profile(self) -> None:
        # a profile with a small change on cluster 1 and a big one on cluster 0: the next step keeps the small one as requested
        s = self.session(**RAISE)
        with self.assertRaises(NeedsRamp) as ctx:
            s.apply(profile(e_core(0, ratio=50), e_core(4, offset_mv=-10)))
        text = ctx.exception.next_profile_text
        entries = load_profile(text).entries
        self.assertEqual([(e.kind, e.index, e.ratio, e.offset_mv) for e in entries], [("E", 0, 49, None), ("E", 4, None, -10)])
        self.assertEqual(s.write_requests(), [])  # the small change is not applied alone: the whole plan was refused


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

    def test_the_ceilings_are_per_kind(self) -> None:
        s = self.session(ratio_max={"E": 49, "P": 58}, v_target_max_mv={"E": 1250})
        with self.assertRaises(PlanRefused):
            s.apply(profile(e_core(0, ratio=50)))  # above the E ceiling
        s = self.session(ratio_max={"E": 49, "P": 58}, v_target_max_mv={"E": 1250})
        self.assertTrue(s.apply(profile('kind = "P"\nindex = 1\nratio = 56')).ok)  # the P ceiling is higher


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
        policy = policy_with(max_ops=400, dwell=0.0, boot_ops=2000, **{**DEFAULT_STEPS, **RAISE})
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
