"""T-023 turbo ratio tables through plan / apply / CLI (simulator only; no hardware).

Rules under test: docs/tasks/T-023-turbo-ratio-tables.md "Deliverables" 4-6 and D-017: a ``[turbo]`` table is written by a guarded plain wrmsr in the
same plan as the per-core words -- a table that raises a bucket **last** (after the voltage), one that only lowers **first** -- counted in the budget,
read back on two CPUs; a mixed table is refused; an E ratio above the current ``0x650`` maximum without a table is warned about.
"""
from __future__ import annotations

import dataclasses
import re
import tempfile
import unittest
from pathlib import Path

from arltuner.controls import (
    NeedsHigherLimit,
    PlanError,
    PlanRefused,
    StepKind,
    StopKind,
    build_plan,
    count_operations,
    format_plan,
    preflight,
    read_steps,
    resolve_targets,
)
from arltuner.guard import MsrAllowEntry, MsrRequest, default_m1_policy, load_policy
from arltuner.msr import SimConfig, SimMsrPort
from arltuner.profiles import Profile, ProfileError, load_profile
from arltuner.topology import arl_s_285k

from ..test_cli import POLICY_TOML, TOKEN, CliCase
from ._support import E_READINGS, P4_READINGS, e_core, make_session, p4, policy_with, profile_text

E_TABLE = 0x3030303030303030
P_TABLE = 0x3737373737373939
TOPO = arl_s_285k()


def table(*buckets: int) -> int:
    return sum(b << (8 * k) for k, b in enumerate(buckets))


def turbo_text(*entries: str, e: list[int] | None = None, p: list[int] | None = None) -> str:
    lines = ["", "[turbo]"]
    if e is not None:
        lines.append(f"e = {e}")
    if p is not None:
        lines.append(f"p = {p}")
    return profile_text(*entries) + "\n".join(lines) + "\n"


def with_turbo_writes(policy):
    base = policy
    extra = (MsrAllowEntry("e_write", 0x650, "write", None, "turbo_ratio_bounded"), MsrAllowEntry("p_write", 0x1AD, "write", None, "turbo_ratio_bounded"))
    return dataclasses.replace(base, msr_allow=base.msr_allow + extra)


def turbo_policy(**changes: object):
    """The test policy (high caps, relaxed steps, default no-raise ceilings) with plain write entries; ``changes`` replace Policy fields."""
    return with_turbo_writes(policy_with(**changes))


def e_plan(text: str, *, policy=None, per_core: bool | None = True, msr_words: dict[int, int] | None = None):
    prof = load_profile(text)
    readings = {c: E_READINGS for c in range(8, 24)}
    readings[4] = P4_READINGS
    words = {0xCE: 0x08040838F1812500, 0x650: E_TABLE, 0x1AD: P_TABLE} if msr_words is None else msr_words
    return build_plan(prof, TOPO, readings, per_core_enabled=per_core, policy=policy or turbo_policy(ratio_max={"P": None, "E": 49}), msr_words=words)


def kinds(plan) -> list[StepKind]:
    return [s.kind for s in plan.steps]


class PlannerOrdering(unittest.TestCase):
    RAISE = turbo_text(e_core(0, ratio=49), e=[49] * 8)
    LOWER = turbo_text(e_core(0, ratio=47), e=[47] * 8)

    def test_a_raise_puts_the_table_last(self) -> None:
        plan = e_plan(self.RAISE)
        ks = kinds(plan)
        self.assertEqual(ks[0:2], [StepKind.READ_MSR, StepKind.READ_MSR])  # 0xCE, then the table
        self.assertEqual([s.msr for s in plan.steps[:2]], [0xCE, 0x650])
        self.assertEqual(ks[-1], StepKind.WRITE_MSR)
        self.assertEqual(ks.count(StepKind.WRITE_MSR), 1)
        last = plan.steps[-1]
        self.assertEqual((last.cpu, last.msr, last.data, last.msr_before, last.verify_cpu), (0, 0x650, table(*([49] * 8)), E_TABLE, 8))
        self.assertTrue(all(s.verdict.allowed for s in plan.steps), plan.refusals)
        self.assertIn("RAISES a turbo ratio table", " ".join(plan.warnings))
        self.assertTrue(plan.steps[-1].number == len(plan.steps))

    def test_a_lowering_puts_the_table_before_the_per_core_words(self) -> None:
        plan = e_plan(self.LOWER)
        ks = kinds(plan)
        first_vf = ks.index(StepKind.WRITE_VF)
        self.assertEqual(ks.index(StepKind.WRITE_MSR), first_vf - 1)
        self.assertEqual(ks.count(StepKind.WRITE_MSR), 1)
        self.assertEqual([s.number for s in plan.steps], list(range(1, len(plan.steps) + 1)))
        self.assertNotIn("RAISES a turbo", " ".join(plan.warnings))

    def test_with_the_percore_enable_unset_the_lowering_follows_the_0x15(self) -> None:
        plan = e_plan(self.LOWER, per_core=False)
        ks = kinds(plan)
        self.assertEqual(ks[ks.index(StepKind.WRITE_PERCORE_ENABLE) + 1], StepKind.WRITE_MSR)

    def test_each_table_is_ordered_by_its_own_direction(self) -> None:
        text = turbo_text(e_core(0, ratio=49), e=[49] * 8, p=[56, 56, 54, 54, 54, 54, 54, 54])  # E raised, P lowered
        plan = e_plan(text, policy=turbo_policy(ratio_max={"P": 58, "E": 49}, max_ratio_step=2))
        order = [(s.msr) for s in plan.steps if s.kind is StepKind.WRITE_MSR]
        self.assertEqual(order, [0x1AD, 0x650])  # the lowering P table first, the raising E table last
        self.assertEqual(kinds(plan)[-1], StepKind.WRITE_MSR)

    def test_a_table_that_raises_and_lowers_is_refused_by_the_planner(self) -> None:
        old = table(48, 48, 48, 48, 48, 48, 48, 48)
        mixed = [49, 49, 48, 48, 48, 48, 47, 47]
        plan = e_plan(turbo_text(e_core(0, ratio=49), e=mixed), policy=turbo_policy(ratio_max={"P": None, "E": 49}))
        step = next(s for s in plan.steps if s.kind is StepKind.WRITE_MSR)
        self.assertFalse(step.verdict.allowed)
        self.assertIn("both raises and lowers", step.verdict.detail)
        self.assertFalse(plan.allowed)
        with self.assertRaises(PlanRefused):
            plan.require_allowed()
        self.assertEqual(old, E_TABLE - 0)  # (documents the baseline used by the helper: 48 x8)

    def test_an_unchanged_table_plans_no_write(self) -> None:
        plan = e_plan(turbo_text(e_core(0, ratio=49), e=[48] * 8))
        self.assertNotIn(StepKind.WRITE_MSR, kinds(plan))
        self.assertIn("no write planned", " ".join(plan.warnings))

    def test_a_table_without_its_baseline_reading_is_a_plan_error(self) -> None:
        with self.assertRaises(PlanError):
            e_plan(self.RAISE, msr_words={0xCE: 0x08040838F1812500})

    def test_the_guard_bounds_are_predicted_per_step(self) -> None:
        # no ceiling configured: the raise is a refused step with the guard's reason
        plan = e_plan(self.RAISE, policy=turbo_policy())
        step = next(s for s in plan.steps if s.kind is StepKind.WRITE_MSR)
        self.assertEqual(step.verdict.reason, "CHECK_FAILED")
        self.assertIn("no-raise", step.verdict.detail)
        # a table above an explicit ceiling, and a bigger step than allowed
        plan = e_plan(turbo_text(e_core(0, ratio=49), e=[50] * 8), policy=turbo_policy(ratio_max={"P": None, "E": 49}))
        self.assertIn("ratio_max", next(s for s in plan.steps if s.kind is StepKind.WRITE_MSR).verdict.detail)
        plan = e_plan(self.RAISE, policy=turbo_policy(ratio_max={"P": None, "E": 49}, max_ratio_step=1))
        self.assertTrue(plan.allowed)

    def test_the_plan_text_shows_the_bytes_before_and_after(self) -> None:
        text = format_plan(e_plan(self.RAISE))
        self.assertIn("48/48/48/48/48/48/48/48 -> 49/49/49/49/49/49/49/49", text)
        self.assertIn("msr 0x650", text)
        self.assertIn("read back on cpu 0 and on cpu 8", text)


class Budget(unittest.TestCase):
    def test_the_plain_writes_count_and_the_plain_reads_do_not(self) -> None:
        base = e_plan(turbo_text(e_core(0, ratio=49), e=[49] * 8))
        plain = e_plan(profile_text(e_core(0, ratio=49)), msr_words={0x650: E_TABLE})
        mailbox = len([s for s in plain.steps if not s.kind.is_msr])
        self.assertEqual(plain.required_operations, mailbox + plain.read_back_requests)
        self.assertEqual(base.required_operations, plain.required_operations + 1)  # the one plain write; 2 plain reads + 2 rdmsr read-backs are free
        self.assertEqual(base.guard_operations, plain.guard_operations + 1)
        self.assertEqual(base.read_back_requests, plain.read_back_requests)

    def test_count_operations_adds_msr_writes(self) -> None:
        targets = resolve_targets(load_profile(profile_text(p4(ratio=56))), TOPO)
        self.assertEqual(count_operations(targets, True, 2), count_operations(targets, True) + 2)

    def test_a_session_that_cannot_afford_the_plain_write_needs_a_higher_limit(self) -> None:
        text = turbo_text(e_core(0, ratio=49), e=[49] * 8)
        prof = load_profile(text)
        targets = resolve_targets(prof, TOPO)
        exact = count_operations(targets, None)
        with self.assertRaises(NeedsHigherLimit) as cm:
            preflight(prof, TOPO, turbo_policy(max_operations_per_session=exact, ratio_max={"P": None, "E": 49}))
        self.assertEqual(cm.exception.required, exact + 1)
        preflight(prof, TOPO, turbo_policy(max_operations_per_session=exact + 1, ratio_max={"P": None, "E": 49}))

    def test_plan_flags_needs_higher_limit_with_the_plain_write(self) -> None:
        plan = e_plan(turbo_text(e_core(0, ratio=49), e=[49] * 8))
        tight = dataclasses.replace(plan, allowed_operations=plan.required_operations - 1)
        self.assertTrue(tight.needs_higher_limit)
        self.assertTrue(dataclasses.replace(plan, allowed_operations=plan.required_operations).allowed)


class ReadsAndWarnings(unittest.TestCase):
    def reads(self, text: str, policy=None):
        prof = load_profile(text)
        return read_steps(resolve_targets(prof, TOPO), policy or turbo_policy(), prof, TOPO)

    def test_a_turbo_profile_reads_0xce_then_each_table_before_any_mailbox_read(self) -> None:
        steps = self.reads(turbo_text(e_core(0, ratio=49), e=[49] * 8, p=[57] * 8))
        self.assertEqual([(s.kind, s.msr, s.cpu) for s in steps[:3]], [(StepKind.READ_MSR, 0xCE, 0), (StepKind.READ_MSR, 0x650, 0), (StepKind.READ_MSR, 0x1AD, 0)])
        self.assertEqual(steps[3].kind, StepKind.READ_PERCORE_ENABLE)

    def test_an_e_ratio_without_a_table_reads_0x650_only_when_the_policy_allows(self) -> None:
        steps = self.reads(profile_text(e_core(0, ratio=49)))
        self.assertEqual([s.msr for s in steps if s.kind is StepKind.READ_MSR], [0x650])
        no_msr = dataclasses.replace(policy_with(), msr_allow=())
        steps = self.reads(profile_text(e_core(0, ratio=49)), no_msr)
        self.assertEqual([s for s in steps if s.kind is StepKind.READ_MSR], [])

    def test_no_plain_read_without_an_e_ratio_or_a_table(self) -> None:
        self.assertEqual([s for s in self.reads(profile_text(p4(offset_mv=-10), e_core(0, offset_mv=-10))) if s.kind.is_msr], [])
        self.assertEqual([s for s in self.reads(profile_text(p4(ratio=56))) if s.kind.is_msr], [])

    def test_a_raise_above_the_table_maximum_without_a_table_warns_that_it_is_capped(self) -> None:
        plan = e_plan(profile_text(e_core(0, ratio=49)), policy=turbo_policy(ratio_max={"P": None, "E": 49}))
        warning = [w for w in plan.warnings if "capped" in w]
        self.assertEqual(len(warning), 1, plan.warnings)
        self.assertIn("ratio 49 is above the E turbo table maximum 48", warning[0])
        self.assertIn("none is invented", warning[0])
        self.assertNotIn(StepKind.WRITE_MSR, kinds(plan))  # never invents a table

    def test_no_capped_warning_when_the_ratio_fits_or_the_profile_has_a_table(self) -> None:
        self.assertFalse([w for w in e_plan(profile_text(e_core(0, ratio=48)), policy=turbo_policy()).warnings if "capped" in w])
        self.assertFalse([w for w in e_plan(turbo_text(e_core(0, ratio=49), e=[49] * 8)).warnings if "capped" in w])
        self.assertFalse([w for w in e_plan(profile_text(p4(ratio=56))).warnings if "capped" in w])
        self.assertFalse([w for w in e_plan(profile_text(e_core(0, offset_mv=-10))).warnings if "capped" in w])

    def test_the_warning_is_once_per_cluster_and_per_target(self) -> None:
        plan = e_plan(profile_text(e_core(0, ratio=49), e_core(4, ratio=49)), policy=turbo_policy(ratio_max={"P": None, "E": 49}))
        self.assertEqual(len([w for w in plan.warnings if "capped" in w]), 2)  # two clusters, one line each


class Preflight(unittest.TestCase):
    def test_the_default_policy_refuses_a_table_before_anything_is_sent(self) -> None:
        prof = load_profile(turbo_text(e_core(0, ratio=48), e=[47] * 8))
        policy = dataclasses.replace(default_m1_policy(), max_operations_per_session=400, max_operations_per_boot=400)
        with self.assertRaises(PlanRefused) as cm:
            preflight(prof, TOPO, policy)
        self.assertTrue(any(r.msr == 0x650 and r.reason == "NOT_ALLOWLISTED" for r in cm.exception.refusals), cm.exception)

    def test_a_table_above_an_explicit_ceiling_is_refused_in_preflight(self) -> None:
        prof = load_profile(turbo_text(e_core(0, ratio=49), e=[50] * 8))
        with self.assertRaises(PlanRefused) as cm:
            preflight(prof, TOPO, turbo_policy(ratio_max={"P": None, "E": 49}))
        self.assertTrue(any("ratio_max" in r.detail and r.msr == 0x650 for r in cm.exception.refusals))

    def test_a_policy_without_the_plain_reads_refuses_the_reads(self) -> None:
        prof = load_profile(turbo_text(e_core(0, ratio=49), e=[49] * 8))
        with self.assertRaises(PlanRefused) as cm:
            preflight(prof, TOPO, dataclasses.replace(turbo_policy(ratio_max={"P": None, "E": 49}), msr_allow=()))
        self.assertTrue(any(r.msr == 0xCE and r.reason == "NOT_ALLOWLISTED" for r in cm.exception.refusals))

    def test_an_allowed_table_passes(self) -> None:
        prof = load_profile(turbo_text(e_core(0, ratio=49), e=[49] * 8))
        self.assertEqual(len(preflight(prof, TOPO, turbo_policy(ratio_max={"P": None, "E": 49}))), 4)


class ApplyThroughTheGuard(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def session(self, **kw):
        policy = kw.pop("policy", None) or turbo_policy(ratio_max={"P": 58, "E": 49})
        return make_session(self.tmp, policy=policy, **kw)

    @staticmethod
    def order(session) -> list[str]:
        """The executed writes in order, from the audit log ('attempt' records; mailbox reads are attempts too, so only write commands count):
        ``mailbox`` for a mailbox write, ``plain 0x650`` for a plain table write."""
        out = []
        for r in session.audit():
            if r["event"] != "attempt":
                continue
            if "msr" in r:
                out.append(f"plain {int(r['msr'], 16):#x}")
            elif int(r["cmd"], 16) in (0x11, 0x15, 0x1D, 0x27):
                out.append("mailbox")
        return out

    def test_a_raise_applies_the_per_core_words_first_and_the_table_last(self) -> None:
        s = self.session()
        report = s.apply(load_profile(turbo_text(e_core(0, ratio=49), e=[49] * 8)))
        self.assertTrue(report.ok, report.format_report())
        order = self.order(s)
        self.assertEqual(order[-1], "plain 0x650")
        self.assertEqual(order.count("plain 0x650"), 1)
        self.assertNotIn("plain 0x650", order[:-1])
        self.assertEqual([s.sim.plain_msr(c, 0x650) for c in (0, 8, 20)], [table(*([49] * 8))] * 3)
        step = report.steps[-1]
        self.assertEqual((step.step.kind, step.verified), (StepKind.WRITE_MSR, True))
        self.assertEqual((step.readback_data, step.verify_readback_data), (table(*([49] * 8)),) * 2)
        self.assertEqual(report.plain_msr_operations, 3)  # 0xCE + 0x650 reads, one write
        self.assertEqual(report.operations, len([r for r in report.steps if not r.step.kind.is_msr]))
        # the revert plan: the table first (a raise is undone frequency-first), restoring the baseline
        self.assertEqual(report.revert_plan[0], MsrRequest(0, 0x650, E_TABLE, "revert", "write", 8))
        self.assertIn("plain wrmsr 0x650 value 0x3030303030303030", report.format_report())

    def test_a_lowering_applies_the_table_first(self) -> None:
        s = self.session(policy=turbo_policy(ratio_max={"P": None, "E": 49}))
        report = s.apply(load_profile(turbo_text(e_core(0, ratio=47), e=[47] * 8)))
        self.assertTrue(report.ok, report.format_report())
        order = self.order(s)
        self.assertEqual(order[:2], ["mailbox", "plain 0x650"])  # the 0x15 enable (the default sim has PerCore Voltage Enable off), then the table
        self.assertEqual(order.count("plain 0x650"), 1)
        self.assertEqual(s.sim.plain_msr(8, 0x650), table(*([47] * 8)))
        self.assertEqual(report.revert_plan[-1], MsrRequest(0, 0x650, E_TABLE, "revert", "write", 8))  # raising a bucket back: after the mailbox undos

    def test_the_p_table_and_both_tables_at_once(self) -> None:
        s = self.session(policy=turbo_policy(ratio_max={"P": 58, "E": 49}, max_ratio_step=2))
        text = turbo_text(p4(ratio=56), e=[49] * 8, p=[58, 58, 56, 56, 56, 56, 56, 56])
        text = text.replace("[turbo]", "[[core]]\nkind = \"E\"\nindex = 0\nratio = 49\n\n[turbo]", 1)
        report = s.apply(load_profile(text))
        self.assertTrue(report.ok, report.format_report())
        self.assertEqual(s.sim.plain_msr(8, 0x1AD), table(58, 58, 56, 56, 56, 56, 56, 56))
        self.assertEqual(s.sim.plain_msr(0, 0x650), table(*([49] * 8)))
        self.assertEqual(len(report.revert_plan) - sum(1 for r in report.revert_plan if not isinstance(r, MsrRequest)), 2)

    def test_a_table_that_does_not_stick_on_the_written_cpu_stops_unverified_and_trips(self) -> None:
        s = self.session()
        s.sim.ignore_plain_writes(0x650)
        report = s.apply(load_profile(turbo_text(e_core(0, ratio=49), e=[49] * 8)))
        self.assertEqual(report.stop_kind, StopKind.UNVERIFIED)
        self.assertTrue(report.tripped)
        self.assertEqual(report.steps[-1].step.kind, StepKind.WRITE_MSR)
        self.assertEqual(len(report.revert_plan), 1 + 1 + 4 * 3)  # the table undo + the 0x15 undo + 3 per-core undos of 4 CPUs

    def test_a_per_cpu_table_stops_unverified_on_the_other_core_kind(self) -> None:
        s = self.session(config=SimConfig(plain_msrs_per_cpu=True))
        report = s.apply(load_profile(turbo_text(e_core(0, ratio=49), e=[49] * 8)))
        self.assertEqual(report.stop_kind, StopKind.UNVERIFIED)
        self.assertIn("cpu 8", report.stopped_reason or "")
        self.assertEqual(s.sim.plain_msr(0, 0x650), table(*([49] * 8)))
        self.assertEqual(s.sim.plain_msr(8, 0x650), E_TABLE)

    def test_a_mixed_table_stops_the_plan_before_any_write(self) -> None:
        s = self.session()
        with self.assertRaises(PlanRefused) as cm:
            s.apply(load_profile(turbo_text(e_core(0, ratio=49), e=[49, 49, 48, 48, 48, 48, 47, 47])))
        self.assertIn("split it", str(cm.exception))
        self.assertEqual(self.order(s), [])  # reads only

    def test_without_a_write_entry_nothing_at_all_is_sent(self) -> None:
        s = self.session(policy=policy_with(ratio_max={"P": None, "E": 49}))  # the default allowlist: plain reads, no plain write
        with self.assertRaises(PlanRefused):
            s.apply(load_profile(turbo_text(e_core(0, ratio=49), e=[49] * 8)))
        self.assertEqual((s.sim.reads, s.sim.writes), ([], []))

    def test_plain_reads_wait_for_no_dwell_and_the_write_is_paced(self) -> None:
        s = self.session()
        report = s.apply(load_profile(turbo_text(e_core(0, ratio=49), e=[49] * 8)))
        self.assertTrue(report.ok)
        guard_operations = report.operations + 1  # every mailbox request and the one plain write; the 2 plain reads are not operations
        dwells = [x for x in s.fake.sleeps if x >= 59]  # the mailbox busy-poll sleeps are far shorter
        self.assertEqual(len(dwells), guard_operations - 1)  # a dwell before every operation but the first

    def test_the_capped_raise_warning_reaches_the_report(self) -> None:
        s = self.session()
        report = s.apply(load_profile(profile_text(e_core(0, ratio=49))))
        self.assertTrue(report.ok, report.format_report())
        self.assertTrue(any("capped" in w for w in report.plan.warnings))  # type: ignore[union-attr]
        self.assertEqual(s.sim.plain_msr(0, 0x650), E_TABLE)  # nothing was written to the table

    def test_the_audit_log_has_the_plain_records(self) -> None:
        s = self.session()
        s.apply(load_profile(turbo_text(e_core(0, ratio=49), e=[49] * 8)))
        recs = s.audit()
        plain = [r for r in recs if "msr" in r]
        self.assertEqual([r["event"] for r in plain if r["event"] in ("msr_read", "attempt")], ["msr_read", "msr_read", "attempt"])
        self.assertEqual(sum(1 for r in plain if r["event"] == "readback"), 2)
        self.assertTrue(all(r["req_data"].startswith("0x") and len(r["req_data"]) == 18 for r in plain if "req_data" in r))


class TurboOnly(unittest.TestCase):
    """G-117: a profile with only a ``[turbo]`` table (the parser still refuses it; the helper builds it from an intent): plain-MSR steps only."""

    @staticmethod
    def profile(**tables: list[int]) -> Profile:
        prof = load_profile(turbo_text(e_core(0, ratio=49), **tables))
        object.__setattr__(prof, "entries", ())  # as the helper does for a table-only intent: Profile insists on one entry at construction
        return prof

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def test_the_operation_count_is_the_plain_writes_alone(self) -> None:
        self.assertEqual(count_operations((), None, 2), 2)
        self.assertEqual(count_operations((), None, 1), 1)
        with self.assertRaises(ValueError):
            count_operations((), None, 0)

    def test_the_reads_are_plain_rdmsr_only_no_0x14(self) -> None:
        prof = self.profile(p=[57] * 8)
        steps = read_steps(resolve_targets(prof, TOPO), turbo_policy(), prof, TOPO)
        self.assertEqual([s.kind for s in steps], [StepKind.READ_MSR] * 2)
        self.assertEqual([s.msr for s in steps], [0xCE, 0x1AD])

    def test_the_plan_has_the_table_write_and_no_0x15(self) -> None:
        prof = self.profile(e=[47] * 8)
        plan = build_plan(prof, TOPO, {}, per_core_enabled=None, policy=turbo_policy(ratio_max={"P": None, "E": 49}), msr_words={0xCE: 0x08040838F1812500, 0x650: E_TABLE})
        self.assertEqual(kinds(plan), [StepKind.READ_MSR, StepKind.READ_MSR, StepKind.WRITE_MSR])
        self.assertTrue(plan.allowed)
        self.assertEqual(plan.required_operations, 1)

    def test_preflight_judges_the_table_against_the_policy(self) -> None:
        self.assertEqual(preflight(self.profile(e=[47] * 8), TOPO, turbo_policy(ratio_max={"P": None, "E": 49})), ())
        with self.assertRaises(PlanRefused):
            preflight(self.profile(e=[50] * 8), TOPO, turbo_policy(ratio_max={"P": None, "E": 49}))
        with self.assertRaises(PlanRefused):  # no msr_allow write entry: default deny
            preflight(self.profile(e=[47] * 8), TOPO, policy_with(ratio_max={"P": None, "E": 49}))

    def test_apply_sends_no_mailbox_request_and_reads_back_on_two_cpus(self) -> None:
        s = make_session(self.tmp, policy=turbo_policy(ratio_max={"P": None, "E": 49}))
        report = s.apply(self.profile(e=[47] * 8))
        self.assertTrue(report.ok, report.format_report())
        self.assertEqual([r.step.kind for r in report.steps], [StepKind.READ_MSR, StepKind.READ_MSR, StepKind.WRITE_MSR])
        self.assertEqual(report.operations, 0)  # no mailbox operation reached the transport
        self.assertEqual(s.sim.plain_msr(0, 0x650), table(*([47] * 8)))
        self.assertEqual(report.steps[-1].verify_readback_data, table(*([47] * 8)))
        self.assertFalse([r for r in s.audit() if r["event"] == "attempt" and "msr" not in r])


class CliTurbo(CliCase):
    POLICY = POLICY_TOML + """
[[msr_allow]]
name = "r_ce"
msr = 0xCE
kind = "read"

[[msr_allow]]
name = "r_650"
msr = 0x650
kind = "read"

[[msr_allow]]
name = "w_650"
msr = 0x650
kind = "write"
cpus = [0]
check = "turbo_ratio_bounded"
"""

    def setUp(self) -> None:
        super().setUp()
        text = self.POLICY.replace("min_dwell_s = 60.0\n", "min_dwell_s = 60.0\nratio_max = { E = 49 }\n", 1)
        self.turbo_policy = self.write("turbo-policy.toml", text)
        self.turbo_profile = self.write("turbo.toml", turbo_text('kind = "E"\nindex = 0\nratio = 49', e=[49] * 8))

    def test_plan_shows_the_table_before_and_after_and_exits_0(self) -> None:
        code, out, err = self.run_cli("plan", self.turbo_profile, "--sim", "--policy", self.turbo_policy)
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("48/48/48/48/48/48/48/48 -> 49/49/49/49/49/49/49/49", out)
        self.assertIn("ALLOWED", out)
        self.assertIn("nothing was sent", out)
        self.assertEqual(self.sim.writes, [])
        self.assertEqual(self.sim.reads, [])  # the plan peeks at the simulator; it is not a device read

    def test_plan_under_the_default_policy_is_refused(self) -> None:
        code, out, err = self.run_cli("plan", self.turbo_profile, "--sim")
        self.assertEqual(code, 2, out + err)
        self.assertIn("REFUSED", out)

    def test_apply_with_the_policy_writes_the_table_last_and_prints_it(self) -> None:
        args = ["apply", self.turbo_profile, "--sim", "--policy", self.turbo_policy, "--audit", self.audit, "--lock", self.lock]
        code, out, err = self.run_cli(*args, stdin=self.confirmed())
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("Result: OK", out)
        self.assertIn("plain wrmsr, read back on cpu 0 and cpu 8", out)
        self.assertEqual([self.sim.plain_msr(c, 0x650) for c in (0, 8)], [table(*([49] * 8))] * 2)
        self.assertTrue(re.search(r"Revert plan.*\n\s+1\. cpu 0 plain wrmsr 0x650 value 0x3030303030303030", out))

    def test_apply_under_the_default_policy_is_refused_before_the_prompt(self) -> None:
        args = ["apply", self.turbo_profile, "--sim", "--audit", self.audit, "--lock", self.lock]
        code, out, err = self.run_cli(*args, stdin=self.confirmed())
        self.assertEqual(code, 2, out + err)
        self.assertNotIn("CONFIRM", out)
        self.assertEqual(self.sim.writes, [])

    def test_an_invalid_turbo_table_is_a_usage_error(self) -> None:
        bad = self.write("bad.toml", turbo_text('kind = "E"\nindex = 0\nratio = 49', e=[49] * 7))
        code, _, err = self.run_cli("plan", bad, "--sim", "--policy", self.turbo_policy)
        self.assertEqual(code, 1, err)

    def test_the_commented_example_entries_of_the_example_policy_load_when_uncommented(self) -> None:
        text = (Path(__file__).resolve().parents[2] / "examples" / "reference-285k" / "policies" / "example-oc-ramp.toml").read_text(encoding="utf-8")
        block = text.split("# Uncomment to allow", 1)[-1].split("# [[msr_allow]]", 1)
        self.assertEqual(len(block), 2)
        tail = "# [[msr_allow]]" + block[1].split("# (for the P table", 1)[0]
        live = "\n".join(line[2:] if line.startswith("# ") else line.lstrip("#") for line in tail.splitlines())
        policy = load_policy(text + "\n" + live)
        self.assertEqual({(e.msr, e.kind) for e in policy.msr_allow}, {(0xCE, "read"), (0x650, "read"), (0x650, "write")})


class ProfileSchema(unittest.TestCase):
    def test_turbo_tables_parse_and_validate(self) -> None:
        p = load_profile(turbo_text(p4(ratio=56), e=[49] * 8, p=[57, 57, 55, 55, 55, 55, 55, 55]))
        self.assertEqual(p.turbo.e, (49,) * 8)  # type: ignore[union-attr]
        self.assertEqual(p.turbo.p, (57, 57, 55, 55, 55, 55, 55, 55))  # type: ignore[union-attr]
        self.assertIsNone(load_profile(profile_text(p4(ratio=56))).turbo)

    def test_bad_tables_are_profile_errors(self) -> None:
        for bad in (
            turbo_text(p4(ratio=56), e=[49] * 7),
            turbo_text(p4(ratio=56), e=[49] * 9),
            turbo_text(p4(ratio=56), e=[0] + [49] * 7),
            turbo_text(p4(ratio=56), e=[256] + [49] * 7),
            turbo_text(p4(ratio=56), p=[True] * 8),
            profile_text(p4(ratio=56)) + "\n[turbo]\n",
            profile_text(p4(ratio=56)) + "\n[turbo]\nx = [1]\n",
            profile_text(p4(ratio=56)) + '\n[turbo]\ne = "49"\n',
        ):
            with self.assertRaises(ProfileError, msg=bad):
                load_profile(bad)

    def test_a_turbo_only_profile_loads(self) -> None:  # G-119: turbo-only intents are real profiles (it used to be refused as empty)
        prof = load_profile('[meta]\nname = "t"\nplatform = "arl-s-285k"\n\n[turbo]\ne = [49, 49, 49, 49, 49, 49, 49, 49]\n')
        self.assertEqual((prof.entries, prof.points, prof.turbo.e), ((), (), (49,) * 8))


if __name__ == "__main__":
    unittest.main()
