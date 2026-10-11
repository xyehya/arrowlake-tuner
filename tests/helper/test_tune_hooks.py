"""G-112: the two optional hooks of ``controls.apply_profile`` the helper's ``tune.apply`` uses (``plan_check``, ``cancelled``), and that without them
nothing changes. Simulator behind the real guard (tests/controls/_support); no hardware."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from arltuner.controls import ApplyPlan, StepKind, StepStatus, StopKind
from arltuner.msr import SimConfig

from tests.controls._support import make_session, point_entry, point_profile, policy_with, with_point_entries


class HookCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        policy = with_point_entries(policy_with(offset_ceiling_mv=200, effective_offset_floor_mv=-200, assumed_bios_offset_mv={"P": 0, "E": 0}))
        self.s = make_session(self.tmp, config=SimConfig.measured(per_point=True), policy=policy)
        self.profile = point_profile(point_entry("P", 0, 5, -60, 53), point_entry("P", 3, 5, -60, 53), name="hooks")

    def writes(self) -> list[tuple[int, int, int]]:
        return self.s.write_requests()


class PlanCheck(HookCase):
    def test_plan_check_sees_the_plan_before_the_first_write(self) -> None:
        seen: list[ApplyPlan] = []
        report = self.s.apply(self.profile, plan_check=seen.append)
        self.assertTrue(report.ok)
        self.assertEqual(len(seen), 1)
        self.assertEqual(sum(1 for st in seen[0].steps if st.kind is StepKind.WRITE_VF_POINT), 2)
        self.assertEqual(len(self.writes()), 2)

    def test_plan_check_raising_means_nothing_was_written(self) -> None:
        class Stale(Exception):
            pass

        def check(plan: ApplyPlan) -> None:
            raise Stale("changed")

        with self.assertRaises(Stale):
            self.s.apply(self.profile, plan_check=check)
        self.assertEqual(self.writes(), [])  # the reads were sent, no write
        self.assertTrue(self.s.requests())

    def test_without_the_hooks_the_behaviour_is_the_old_one(self) -> None:
        report = self.s.apply(self.profile)
        self.assertTrue(report.ok)
        self.assertEqual(report.stop_kind, StopKind.NONE)


class Cancel(HookCase):
    def test_cancel_in_the_read_phase_sends_no_write(self) -> None:
        report = self.s.apply(self.profile, cancelled=lambda: True)
        self.assertFalse(report.ok)
        self.assertEqual(report.stop_kind, StopKind.CANCELLED)
        self.assertEqual(report.steps[-1].status, StepStatus.CANCELLED)
        self.assertEqual(self.s.requests(), [])  # not even the first read: the flag was set before it
        self.assertEqual(report.operations, 0)

    def test_cancel_between_two_writes_stops_after_the_first(self) -> None:
        calls = {"n": 0, "armed": False}

        def cancelled() -> bool:
            if calls["armed"] and len(self.writes()) >= 1:
                return True
            return False

        def check(plan: ApplyPlan) -> None:
            calls["armed"] = True  # from the first write phase on

        report = self.s.apply(self.profile, plan_check=check, cancelled=cancelled)
        self.assertEqual(report.stop_kind, StopKind.CANCELLED)
        self.assertEqual(len(self.writes()), 1)  # one write sent and read back; the second never left
        done = [r for r in report.steps if r.step.kind.is_write and r.status is StepStatus.OK]
        self.assertEqual(len(done), 1)
        self.assertTrue(done[0].verified)
        self.assertEqual(report.steps[-1].status, StepStatus.CANCELLED)
        self.assertEqual(report.operations, len([r for r in report.steps if r.status is not StepStatus.CANCELLED]))  # the cancelled step is not counted
        self.assertTrue(report.revert_plan)  # the revert plan of what was written is still reported

    def test_a_flag_that_never_sets_changes_nothing(self) -> None:
        report = self.s.apply(self.profile, cancelled=lambda: False)
        self.assertTrue(report.ok)


if __name__ == "__main__":
    unittest.main()
