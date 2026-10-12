"""T-025 controls tests: per-V/F-point offsets (``[[point]]``) through the planner, ``apply_profile``, the REAL guard and the simulator.

Measured (``[MEASURED lnx 2026-10-02]``, examples/reference-285k/notes/platform.md section 3, E CPU 8): ``0x10 P2=6`` = ``0x0000002E`` (46x, V target 0, offset 0), ``0x10
P2=7`` -> rc ``0x11``. Whether the hardware accepts a point write on Linux is ``[UNVERIFIED]``: these tests prove what arltuner sends, in which order, and
what it refuses -- against the simulator's own SIM-ONLY rule set.
"""
from __future__ import annotations

import dataclasses
import tempfile
import unittest
from pathlib import Path

from arltuner.controls import (
    ApplyReport,
    NeedsBootBudget,
    NeedsHigherLimit,
    NeedsRamp,
    PlanError,
    PlanRefused,
    StepKind,
    StopKind,
    build_plan,
    count_operations,
    format_plan,
    format_preview,
    format_revert_plan,
    preflight,
    read_steps,
    resolve_point_targets,
    resolve_targets,
)
from arltuner.controls.planner import POINT_CAVEAT
from arltuner.guard import AllowEntry, MsrAllowEntry
from arltuner.mailbox import Cmd, VfWord
from arltuner.msr import SimConfig
from arltuner.profiles import load_profile
from arltuner.topology import arl_s_285k

from ._support import DEFAULT_STEPS, P4_READINGS, e_core, make_session, p4, point_entry, point_profile, point_text, policy_with, profile, with_point_entries

POINT6 = 0x0000002E
E_CPUS = (8, 9, 10, 11)


def point_word(ratio: int, offset_raw: int) -> int:
    return VfWord(ratio, 0, False, offset_raw).to_u32()


def sim_cfg(**overrides: object) -> SimConfig:
    """The measured machine with V/F points on (E 6 / P 8 points; point 6 of an E CPU reads 46x)."""
    overrides.setdefault("point_ratios", {("E", 6): 46, ("P", 8): 57})  # P point 8 is a SIM-ONLY value; only E point 6 = 46 is measured
    return SimConfig.measured(per_point=True, **overrides)


class PointCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self._n = 0

    def session(self, *, steps=None, config=None, points=True, **policy_changes):
        self._n += 1
        sub = self.tmp / f"s{self._n}"
        sub.mkdir()
        policy = policy_with(max_ops=400, dwell=0.0, boot_ops=2000, **({} if steps is None else steps), **policy_changes)
        if points:
            policy = with_point_entries(policy)
        return make_session(sub, config=sim_cfg() if config is None else config, policy=policy)

    @staticmethod
    def log(s, cpu: int) -> list[tuple[int, int]]:
        return [(c, d) for cpu_, c, d in s.requests() if cpu_ == cpu]

    @staticmethod
    def writes_of(report: ApplyReport, kind: StepKind):
        return [r for r in report.steps if r.step.kind is kind]


class InertByDefault(PointCase):
    def test_the_shipped_policy_refuses_a_point_profile_before_anything_is_sent(self) -> None:
        s = self.session(points=False)  # default M1 allowlist: no 0x10 / 0x11 with P2 != 0
        with self.assertRaises(PlanRefused) as ctx:
            s.apply(point_profile(point_entry("E", 0, 6, -20)))
        reasons = {(r.cmd, r.reason) for r in ctx.exception.refusals}
        self.assertEqual(reasons, {(0x10, "NOT_ALLOWLISTED"), (0x11, "NOT_ALLOWLISTED")})
        self.assertEqual(s.requests(), [])  # not even the 0x14 read

    def test_the_plan_of_such_a_profile_is_not_allowed(self) -> None:
        policy = policy_with()
        plan = build_plan(
            point_profile(point_entry("E", 0, 6, -20)), arl_s_285k(), {}, per_core_enabled=True, policy=policy, point_words={(c, 6): POINT6 for c in E_CPUS}
        )
        self.assertFalse(plan.allowed)
        point_steps = [s for s in plan.steps if s.p2 == 6]
        self.assertEqual(len(point_steps), 8)  # 4 reads + 4 writes
        self.assertTrue(all(s.verdict.reason == "NOT_ALLOWLISTED" for s in point_steps))

    def test_a_policy_with_only_the_point_reads_still_refuses_the_write(self) -> None:
        s = self.session(points=False)
        policy = dataclasses.replace(
            s.policy, allow=(*s.policy.allow, AllowEntry("vf_point_read", Cmd.VOLTAGE_FREQ_READ, frozenset({0}), frozenset(range(1, 16)), "read"))
        )
        with self.assertRaises(PlanRefused) as ctx:
            preflight(point_profile(point_entry("E", 0, 6, -20)), arl_s_285k(), policy)
        self.assertEqual({r.cmd for r in ctx.exception.refusals}, {0x11})


class OnePointEndToEnd(PointCase):
    def test_a_point_whose_baseline_ratio_reads_zero_is_refused_not_guessed(self) -> None:
        s = self.session()
        with self.assertRaises(PlanRefused) as ctx:
            s.apply(point_profile(point_entry("P", 4, 1, -5)))  # the model gives P point 1 no ratio: 0x10 reads 0x0
        self.assertIn("ratio 0", ctx.exception.refusals[0].detail)
        self.assertEqual([c for c in s.sim.requests if c.cmd == 0x11], [])

    def test_an_e_cluster_point_is_read_written_and_read_back_on_every_cpu(self) -> None:
        s = self.session()
        report = s.apply(point_profile(point_entry("E", 0, 6, -20, expect_ratio=46)))
        self.assertTrue(report.ok, report.format_report())
        new = point_word(46, -20)
        for cpu in E_CPUS:
            self.assertEqual(
                self.log(s, cpu),
                [*([(0x14, 0)] if cpu == 8 else []), (0x10, 0), (0x11, new), (0x10, 0)],  # read, write, the guard's read-back; nothing else
                cpu,
            )
            self.assertEqual(s.sim.vf(cpu, 0, 6), VfWord(46, 0, False, -20), cpu)
        self.assertEqual(s.sim.vf(12, 0, 6), VfWord(46, 0, False, 0))  # the next cluster was not touched
        points = self.writes_of(report, StepKind.WRITE_VF_POINT)
        self.assertEqual(len(points), 4)
        self.assertTrue(all(r.verified and r.status.value == "ok" for r in points))
        self.assertEqual({r.applied_offset_mv for r in points}, {VfWord.from_u32(new).offset_mv})
        self.assertEqual([c for c in s.sim.requests if c.cmd in (0x1D, 0x27)], [])  # no 0x1D / 0x27 for a point

    def test_only_the_read_and_the_write_belong_to_a_point(self) -> None:
        s = self.session()
        report = s.apply(point_profile(point_entry("P", 4, 8, -5)))
        self.assertTrue(report.ok, report.format_report())
        self.assertEqual(self.log(s, 4), [(0x14, 0), (0x10, 0), (0x11, point_word(57, -5)), (0x10, 0)])

    def test_the_oc_point_word_is_never_touched(self) -> None:
        s = self.session()
        before = s.sim.vf(8)
        s.apply(point_profile(point_entry("E", 0, 6, -20)))
        self.assertEqual(s.sim.vf(8), before)

    def test_the_0x15_enable_follows_the_core_rule(self) -> None:
        cfg = SimConfig(per_point=True, point_counts={"E": 6, "P": 8})  # unmeasured: PerCore Voltage Enable (0x14 bit 3) is clear
        s = self.session(config=cfg)
        report = s.apply(point_profile(point_entry("E", 0, 6, -20)))
        self.assertTrue(report.ok, report.format_report())
        self.assertEqual(self.log(s, 8)[:3], [(0x14, 0), (0x10, 0), (0x15, 0x8)])  # all reads first, then the 0x15 (old|8), then the point writes
        self.assertTrue(s.sim.per_core_enabled)

    def test_a_point_only_profile_needs_no_core_target(self) -> None:
        self.assertEqual(resolve_targets(point_profile(point_entry("E", 0, 6, -20)), arl_s_285k()), ())
        self.assertEqual(len(resolve_point_targets(point_profile(point_entry("E", 0, 6, -20)), arl_s_285k())), 4)


class ProfilesAreTheSameWithoutPoints(PointCase):
    def test_a_core_only_profile_plans_and_runs_exactly_as_before(self) -> None:
        s = self.session()
        report = s.apply(profile(p4(offset_mv=-20)))
        self.assertTrue(report.ok, report.format_report())
        self.assertEqual([r.step.kind for r in report.steps if r.step.kind in (StepKind.READ_VF_POINT, StepKind.WRITE_VF_POINT)], [])
        self.assertEqual(report.plan.point_targets, ())
        self.assertNotIn(POINT_CAVEAT, report.plan.warnings)
        self.assertIn("XTU sent on Windows", " ".join(report.plan.warnings))


class Ordering(PointCase):
    def test_points_are_written_before_the_core_words_and_all_reads_before_any_write(self) -> None:
        s = self.session(offset_ceiling_mv=0)
        prof = point_profile(point_entry("P", 4, 8, -5), point_entry("E", 0, 6, -20), entries=(p4(offset_mv=-30),))
        report = s.apply(prof)
        self.assertTrue(report.ok, report.format_report())
        kinds = [r.step.kind for r in report.steps]
        first_write = next(i for i, k in enumerate(kinds) if k.is_write)
        self.assertTrue(all(not k.is_write for k in kinds[:first_write]))
        writes = [(r.step.kind, r.step.cpu, r.step.p2) for r in report.steps if r.step.kind.is_write]
        self.assertEqual(
            writes,
            [
                (StepKind.WRITE_VF_POINT, 4, 8),
                *[(StepKind.WRITE_VF_POINT, c, 6) for c in E_CPUS],
                (StepKind.WRITE_VF, 4, 0),
            ],
        )
        reads = [(r.step.kind, r.step.cpu, r.step.p2) for r in report.steps if not r.step.kind.is_write]
        self.assertEqual(reads[0][0], StepKind.READ_PERCORE_ENABLE)
        self.assertEqual(reads[0][1], 4)  # 0x14 on the first CPU the plan touches: the first point target
        self.assertEqual([k for k, _, _ in reads[1:6]], [StepKind.READ_VF_POINT] * 5)  # the point reads follow 0x14, then the core reads

    def test_a_core_and_a_point_of_the_same_core_do_not_disturb_each_other(self) -> None:
        s = self.session()
        report = s.apply(point_profile(point_entry("E", 0, 6, -20), entries=(e_core(0, offset_mv=-10),)))
        self.assertTrue(report.ok, report.format_report())
        for cpu in E_CPUS:
            self.assertEqual(s.sim.vf(cpu, 0, 6), VfWord(46, 0, False, -20), cpu)
            self.assertEqual(s.sim.vf(cpu).offset_raw, -10, cpu)  # the whole-curve offset (P2 = 0) is its own word
            self.assertEqual(s.sim.vf(cpu).ratio, 46, cpu)  # G-122: no OC point (48 would add one)

    def test_a_lowering_turbo_table_comes_before_the_points_and_a_raising_one_after_the_cores(self) -> None:
        # documented order: 0x15, lowering-only turbo table, point writes, core words, raising turbo table
        topo = arl_s_285k()
        policy = with_point_entries(policy_with(ratio_max={"P": None, "E": 49}))
        policy = dataclasses.replace(
            policy,
            msr_allow=policy.msr_allow
            + (MsrAllowEntry("e_write", 0x650, "write", None, "turbo_ratio_bounded"), MsrAllowEntry("p_write", 0x1AD, "write", None, "turbo_ratio_bounded")),
        )
        words = {0xCE: 0x08040838F1812500, 0x650: 0x3030303030303030, 0x1AD: 0x3737373737373939}
        points = {(c, 6): POINT6 for c in E_CPUS}
        for table, where in (("e = [47, 47, 47, 47, 47, 47, 47, 47]", "lower"), ("e = [49, 49, 49, 49, 49, 49, 49, 49]", "raise")):
            with self.subTest(table=where):
                text = point_text(point_entry("E", 0, 6, -20), entries=(p4(offset_mv=-30),)) + "\n[turbo]\n" + table + "\n"
                plan = build_plan(load_profile(text), topo, {4: P4_READINGS}, per_core_enabled=True, policy=policy, msr_words=words, point_words=points)
                self.assertTrue(plan.allowed, plan.refusals)
                order = [s.kind for s in plan.steps if s.kind.is_write]
                table_at = order.index(StepKind.WRITE_MSR)
                first_point = order.index(StepKind.WRITE_VF_POINT)
                first_core = order.index(StepKind.WRITE_VF)
                self.assertLess(first_point, first_core)
                if where == "lower":
                    self.assertLess(table_at, first_point)
                else:
                    self.assertGreater(table_at, first_core)
                    self.assertEqual(table_at, len(order) - 1)
                self.assertEqual(plan.required_operations, count_operations(resolve_targets(load_profile(text), topo), True, 1, 4))


class ExpectRatio(PointCase):
    def test_a_ratio_that_differs_from_the_read_refuses_the_write_and_sends_none(self) -> None:
        s = self.session()
        with self.assertRaises(PlanRefused) as ctx:
            s.apply(point_profile(point_entry("E", 0, 6, -20, expect_ratio=45)))
        (first, *_) = ctx.exception.refusals
        self.assertEqual((first.cmd, first.reason), (0x11, "CHECK_FAILED"))
        self.assertIn("expect_ratio", first.detail)
        self.assertIn("is 46", first.detail)
        self.assertEqual([c for c in s.sim.requests if c.cmd in (0x11, 0x15)], [])  # reads yes, writes never
        self.assertEqual(s.sim.vf(8, 0, 6), VfWord(46, 0, False, 0))
        report = ctx.exception.report
        assert report is not None
        self.assertFalse(any(r.step.kind.is_write for r in report.steps))

    def test_a_matching_or_absent_expect_ratio_passes(self) -> None:
        for expect in (46, None):
            with self.subTest(expect=expect):
                s = self.session()
                self.assertTrue(s.apply(point_profile(point_entry("E", 0, 6, -20, expect))).ok)


class Bounds(PointCase):
    def test_an_offset_outside_the_policy_window_is_refused_in_preflight_before_any_request(self) -> None:
        for mv, ok in ((-100, True), (-101, False), (5, False)):
            with self.subTest(mv=mv):
                s = self.session()
                prof = point_profile(point_entry("E", 0, 6, mv))
                if ok:
                    self.assertIsInstance(preflight(prof, arl_s_285k(), s.policy), tuple)
                    continue
                with self.assertRaises(PlanRefused) as ctx:
                    s.apply(prof)
                self.assertIn("vf_point_offset_bounded", ctx.exception.refusals[0].detail)
                self.assertEqual(s.requests(), [])

    def test_a_positive_offset_works_with_a_raised_ceiling_and_warns_about_the_raise(self) -> None:
        s = self.session(offset_ceiling_mv=20)
        report = s.apply(point_profile(point_entry("P", 4, 8, 10)))
        self.assertTrue(report.ok, report.format_report())
        self.assertTrue(any("RAISES" in w for w in report.plan.warnings))

    def test_a_step_larger_than_the_limit_gets_the_next_allowed_step_as_a_profile(self) -> None:
        s = self.session(steps=DEFAULT_STEPS)  # 25 mV per word
        with self.assertRaises(NeedsRamp) as ctx:
            s.apply(point_profile(point_entry("E", 0, 6, -60, expect_ratio=46)))
        nxt = load_profile(ctx.exception.next_profile_text)
        self.assertEqual(nxt.entries, ())
        (entry,) = nxt.points
        self.assertEqual((entry.kind, entry.index, entry.point, entry.expect_ratio), ("E", 0, 6, 46))
        self.assertEqual(VfWord.mv_to_offset_raw(entry.offset_mv), -26)  # 25 mV = 26 raw: the step limit, not beyond it
        self.assertEqual([c for c in s.sim.requests if c.cmd == 0x11], [])  # nothing written
        # and the next step is itself allowed and applies
        s2 = self.session(steps=DEFAULT_STEPS)
        self.assertTrue(s2.apply(nxt).ok)
        self.assertEqual(s2.sim.vf(8, 0, 6).offset_raw, -26)

    def test_a_hard_refusal_beside_a_step_refusal_offers_no_ramp(self) -> None:
        s = self.session(steps=DEFAULT_STEPS)
        prof = point_profile(point_entry("E", 0, 6, -60), point_entry("P", 4, 8, -300))  # -300 mV is below any window
        with self.assertRaises(PlanRefused):  # preflight sees the absolute bound first
            s.apply(prof)


class Budget(PointCase):
    def test_count_operations_counts_a_read_a_write_and_a_read_back_per_point_cpu(self) -> None:
        self.assertEqual(count_operations((), True, 0, 4), 1 + 4 + 4 + 4)  # 0x14, 4 reads, 4 writes, 4 read-backs
        self.assertEqual(count_operations((), None, 0, 4), 1 + 4 + (4 + 1) + (4 + 1))  # the worst case adds the 0x15 and its read-back
        self.assertEqual(count_operations(1, None, 0, 0), count_operations(1, None))
        with self.assertRaises(ValueError):
            count_operations((), None, 0, 0)  # nothing to do is still an error
        with self.assertRaises(ValueError):
            count_operations((), None, 0, -1)

    def test_a_mixed_profile_adds_up(self) -> None:
        prof = point_profile(point_entry("E", 0, 6, -20), entries=(e_core(0, offset_mv=-10),))
        targets = resolve_targets(prof, arl_s_285k())
        self.assertEqual(count_operations(targets, True, 0, 4), count_operations(targets, True) + 4 * 3)

    def test_the_session_limit_and_the_boot_budget_cover_the_points(self) -> None:
        prof = point_profile(point_entry("E", 0, 6, -20))
        topo = arl_s_285k()
        need = count_operations((), None, 0, 4)
        with self.assertRaises(NeedsHigherLimit) as hl:
            preflight(prof, topo, with_point_entries(policy_with(max_ops=need - 1)))
        self.assertEqual(hl.exception.required, need)
        self.assertIsInstance(preflight(prof, topo, with_point_entries(policy_with(max_ops=need))), tuple)
        with self.assertRaises(NeedsBootBudget):
            preflight(prof, topo, with_point_entries(policy_with(boot_ops=need)), boot_ops_used=1)

    def test_the_plan_reports_the_same_budget_as_the_applied_run(self) -> None:
        s = self.session()
        report = s.apply(point_profile(point_entry("E", 0, 6, -20)))
        plan = report.plan
        assert plan is not None
        self.assertEqual(plan.required_operations, 1 + 4 + 4 + 4)  # the measured machine has PerCore Voltage Enable set: no 0x15
        self.assertEqual(report.operations, plan.guard_operations)


class PlanText(PointCase):
    def plan(self):
        s = self.session()
        return build_plan(
            point_profile(point_entry("E", 0, 6, -20, 46)),
            arl_s_285k(),
            {},
            per_core_enabled=True,
            policy=s.policy,
            point_words={(c, 6): POINT6 for c in E_CPUS},
        )

    def test_the_plan_shows_point_ratio_and_the_offset_before_and_after(self) -> None:
        plan = self.plan()
        self.assertTrue(plan.allowed, plan.refusals)
        text = format_plan(plan)
        for needle in (
            "point 6 offset -20 mV (expects ratio 46)",
            "write V/F point 6 offset of E-Core 0",
            "ratio 46 (kept)",
            "V target raw 0x0 (kept)",
            "offset +0.00 -> -19.53 mV (raw 0 -> -20)",
            "cmd 0x11",
            "Predicted verdict: ALLOWED",
        ):
            self.assertIn(needle, text)
        self.assertEqual(text.count("write V/F point 6 offset"), 4)
        self.assertNotIn("cmd 0x1D", text)
        self.assertNotIn("cmd 0x27", text)

    def test_the_monotonicity_warning_is_on_every_point_plan(self) -> None:
        plan = self.plan()
        self.assertIn(POINT_CAVEAT, plan.warnings)
        self.assertIn("V(n) >= V(n-1)", POINT_CAVEAT)
        self.assertIn("not check", POINT_CAVEAT)
        self.assertIn(f"WARNING: {POINT_CAVEAT}", format_plan(plan))

    def test_the_preview_of_a_real_apply_names_the_point_requests(self) -> None:
        s = self.session()
        prof = point_profile(point_entry("E", 0, 6, -20, 46))
        text = format_preview(prof.name, resolve_targets(prof, arl_s_285k()), s.policy, None, resolve_point_targets(prof, arl_s_285k()))
        self.assertIn("point 6 offset -20 mV (expects ratio 46)", text)
        self.assertIn("0x10 P2=<point>", text)
        self.assertIn("read 0x14 on cpu 8", text)
        self.assertNotIn("0x1C", text)  # a point-only profile reads no 0x1C / 0x09 / 0x26
        self.assertIn("at most 15 mailbox WRMSRs", text)  # 0x14, 4 reads, (4 + 0x15) writes, as many read-backs

    def test_read_steps_of_a_point_profile(self) -> None:
        s = self.session()
        prof = point_profile(point_entry("E", 0, 6, -20))
        steps = read_steps(resolve_targets(prof, arl_s_285k()), s.policy, prof, arl_s_285k())
        self.assertEqual([(st.kind, st.cpu, st.cmd, st.p2) for st in steps], [(StepKind.READ_PERCORE_ENABLE, 8, 0x14, 0), *[(StepKind.READ_VF_POINT, c, 0x10, 6) for c in E_CPUS]])
        self.assertTrue(all(st.verdict.allowed for st in steps))

    def test_missing_point_words_are_a_plan_error(self) -> None:
        with self.assertRaises(PlanError):
            build_plan(point_profile(point_entry("E", 0, 6, -20)), arl_s_285k(), {}, per_core_enabled=True, policy=with_point_entries(policy_with()), point_words={})


class AfterTheWrite(PointCase):
    def test_a_point_that_does_not_take_the_word_is_unverified_and_trips_the_guard(self) -> None:
        s = self.session()
        s.sim.silent_ignore(0x11)  # rc 0 but nothing stored: the read-back differs
        report = s.apply(point_profile(point_entry("E", 0, 6, -20)))
        self.assertFalse(report.ok)
        self.assertIs(report.stop_kind, StopKind.UNVERIFIED)
        self.assertTrue(report.tripped)
        self.assertEqual(len(self.writes_of(report, StepKind.WRITE_VF_POINT)), 1)  # stopped at the first one

    def test_the_revert_plan_restores_each_point_word_exactly(self) -> None:
        s = self.session()
        report = s.apply(point_profile(point_entry("E", 0, 6, -20)))
        self.assertTrue(report.ok)
        undo = [(r.cpu, r.cmd, r.p1, r.p2, r.data) for r in report.revert_plan]
        self.assertEqual(sorted(undo), sorted((c, 0x11, 0, 6, POINT6) for c in E_CPUS))  # the baseline word: ratio 46, V target 0, offset 0
        self.assertNotIn("XTU sent on Windows", " ".join(report.plan.warnings))  # the 0x11 / 0x1D / 0x27 shape caveat is about [[core]] writes
        text = "\n".join(format_revert_plan(report.revert_plan))
        self.assertIn("restores the V/F point 6 word as read", text)
        self.assertNotIn("zero ratio / V target is filled in", text)
        for line in report.format_report().splitlines():
            self.assertTrue(line.isascii(), line)

    def test_replaying_the_revert_plan_through_the_guard_restores_the_points(self) -> None:
        s = self.session()
        report = s.apply(point_profile(point_entry("E", 0, 6, -20)))
        for req in report.revert_plan:
            s.fake.sleep(1.0)
            self.assertIs(s.guard.execute(req, s.confirm).verified, True)
        for cpu in E_CPUS:
            self.assertEqual(s.sim.vf(cpu, 0, 6).to_u32(), POINT6, cpu)

    def test_the_report_lists_the_values_as_read_back(self) -> None:
        s = self.session()
        text = s.apply(point_profile(point_entry("E", 0, 6, -20))).format_report()
        self.assertIn("Values as read back", text)
        self.assertIn("write V/F point offset of E-Core 0 point 6", text)
        self.assertIn("read V/F point baseline of", text)


if __name__ == "__main__":
    unittest.main()
