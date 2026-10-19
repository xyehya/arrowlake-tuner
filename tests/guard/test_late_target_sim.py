"""G-237: the whole chain -- ``WriteGuard`` over ``MailboxTransport`` over ``SimMsrPort(late_target_reads=k)`` -- for the late V target of a new OC point
(registers.md "OC-point V target lands late" ``[MEASURED lnx 2026-10-19]``). Simulator only; the guard's own cases are ``LateTarget`` in ``test_guard.py``."""
from __future__ import annotations

import dataclasses
import unittest

from arltuner.guard import Refusal
from arltuner.msr import MailboxTransport, SimConfig, SimMsrPort

from ..msr._support import FakeClock as SimClock
from .test_guard import GuardCase

WORD = 0x0005B43A  # cpu 0 (favored P core, top fused ratio 57 at 0x5B4): 58x, adaptive 1.4258 V -- the live trip


class LateTargetOverTheSimulator(GuardCase):
    def rig(self, late: int):
        self.sim = SimMsrPort(SimConfig.measured(per_point=True, late_target_reads=late))
        fc = SimClock()
        self.waits: list[float] = []
        self.policy = dataclasses.replace(self.policy, hw_v_target_max_mv={"P": 1600, "E": 1600})
        guard = self.make_guard(transport=MailboxTransport(self.sim, sleep=fc.sleep, clock=fc.clock, timeout_s=1.0), settle_wait=self.waits.append)
        for cmd in (0x10, 0x1C, 0x09):
            self.baseline_read(guard, 0, cmd)
        return guard

    def test_lands_at_poll_2(self) -> None:
        guard = self.rig(2)
        result = guard.execute(self.write_req(WORD), self.confirm)
        self.assertIs(result.verified, True)
        self.assertEqual(result.polls, 2)
        self.assertFalse(guard.tripped)
        self.assertEqual(self.waits, [0.002, 0.005])
        self.assertEqual(self.sim.vf(0).to_u32(), WORD)
        self.assertEqual(guard.operations, 3 + 1)  # re-reads not counted (G-237 review f1)

    def test_never_lands_trips_after_four_rereads(self) -> None:
        guard = self.rig(-1)
        result = guard.execute(self.write_req(WORD), self.confirm)
        self.assertIs(result.verified, False)
        self.assertEqual(result.polls, 4)
        self.assertTrue(guard.tripped)
        self.assertEqual(self.waits, [0.002, 0.005, 0.010, 0.020])
        self.assert_refused(guard, self.read_req(), Refusal.TRIPPED)

    def test_without_the_mode_nothing_polls(self) -> None:
        guard = self.rig(0)
        result = guard.execute(self.write_req(WORD), self.confirm)
        self.assertEqual((result.verified, result.polls), (True, 0))
        self.assertEqual(self.waits, [])


if __name__ == "__main__":
    unittest.main()
