"""G-241: an OC point is created by one ratio + voltage word (no voltage-first word exists without an OC point); a plan-time check catches the PlanError the apply would raise
from the same readings; an apply refused before its first write says so (``detail.writes_sent == 0``)."""
from __future__ import annotations

import dataclasses
from unittest import mock

from arltuner.controls import PlanError, PreflightError
from arltuner.mailbox import VfWord
from arltuner.msr import SimConfig

from .test_tune import TuneCase, row, rows

E_CPUS = (8, 9, 10, 11)


def no_oc_point() -> SimConfig:
    """E cluster 0 at the fused top 46x with no OC point (the 2026-10-19 BIOS-defaults state): 0x0000002E."""
    cfg = SimConfig.measured(per_point=True, late_target_reads=2)
    return dataclasses.replace(cfg, initial_vf={**cfg.initial_vf, **{c: VfWord(46, 0, False, 0) for c in E_CPUS}})


class OneWordCreation(TuneCase):
    def test_plan_and_apply_create_the_oc_point_with_one_word_per_cpu(self) -> None:
        h = self.boot(no_oc_point())
        self.read(h)
        plan = self.plan(h, rows(row("EC0", ratio=50, v=1.1367)))
        res = self.apply_ok(h, plan)
        for cpu in E_CPUS:
            words = [r.data for r in self.sim.requests if r.cmd == 0x11 and r.cpu == cpu and r.p2 == 0]
            self.assertEqual(words, [0x00048C32], cpu)  # ONE word: ratio 50 | target 0x48C << 8, offset 0
            self.assertEqual(self.word(cpu).to_u32(), 0x00048C32, cpu)
            order = [r.cmd for r in self.sim.requests if r.cpu == cpu and r.cmd in (0x11, 0x1D, 0x27)]
            self.assertEqual(order, [0x11, 0x1D, 0x27], cpu)  # the ratio is usable only after the 0x1D that follows
        self.assertTrue(all(rb["match"] for rb in res["readback"]), res["readback"])


class PlanCatchesWhatApplyRaises(TuneCase):
    def test_a_plan_error_from_the_last_reads_is_raised_by_tune_plan_not_at_apply(self) -> None:
        h = self.boot(no_oc_point())
        self.read(h)
        with mock.patch("arltuner.helper.ops_tune.build_plan", side_effect=PlanError("EC0: boom")):
            r = h.call("p1", "tune.plan", {"intent": rows(row("EC0", ratio=50, v=1.1367))})
        self.assertFalse(r["ok"], r)
        self.assertIn("boom", r["error"]["message"])

    def test_a_word_the_last_read_does_not_hold_is_left_to_the_apply(self) -> None:
        h = self.boot(no_oc_point())
        self.read(h, cpus=[0])  # EC0 was not read: the plan has no baseline for it
        with mock.patch("arltuner.helper.ops_tune.build_plan", side_effect=PlanError("no baseline readings for CPU(s) [8]")):
            r = h.call("p2", "tune.plan", {"intent": rows(row("EC0", ratio=50, v=1.1367))})
        self.assertTrue(r["ok"], r)


class ApplyRefusedBeforeAnyWrite(TuneCase):
    def test_a_plan_error_at_apply_names_zero_writes(self) -> None:
        h = self.boot(no_oc_point())
        self.read(h)
        plan = self.plan(h, rows(row("EC0", ratio=50, v=1.1367)))
        with mock.patch("arltuner.controls.executor.build_plan", side_effect=PlanError("EC0: boom")):
            r = self.apply(h, plan)
        self.assertFalse(r["ok"], r)
        self.assertIn("Nothing was written", r["error"]["message"])
        self.assertEqual(r["error"]["detail"]["writes_sent"], 0)
        self.assertEqual([x for x in self.sim.requests if x.cmd in (0x11, 0x15, 0x1D, 0x27)], [])

    def test_a_preflight_refusal_of_the_plan_at_apply_names_zero_writes(self) -> None:
        h = self.boot(no_oc_point())
        self.read(h)
        plan = self.plan(h, rows(row("EC0", ratio=50, v=1.1367)))
        with mock.patch("arltuner.controls.model.ApplyPlan.require_allowed", side_effect=PreflightError("the plan would be refused")):
            r = self.apply(h, plan)
        self.assertFalse(r["ok"], r)
        self.assertEqual(r["error"]["detail"]["writes_sent"], 0)
        self.assertEqual([x for x in self.sim.requests if x.cmd in (0x11, 0x15, 0x1D, 0x27)], [])
