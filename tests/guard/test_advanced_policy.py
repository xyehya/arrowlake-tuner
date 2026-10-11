"""D-032: the advanced-user policy file loads with exactly the numbers the operator approved, and the code sanity ceilings allow them (no more)."""
from __future__ import annotations

import unittest
from pathlib import Path

from arltuner.guard import PolicyError, load_policy
from arltuner.guard.policy import SANITY_OFFSET_CEILING_MV, SANITY_V_TARGET_MAX_MV
from arltuner.mailbox import VfWord

POLICY = Path(__file__).resolve().parents[2] / "policies" / "advanced-2026-10-11.toml"


class AdvancedPolicy(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = load_policy(POLICY)

    def test_numbers_are_the_d032_ones(self) -> None:
        p = self.policy
        self.assertEqual((p.offset_ceiling_mv, p.effective_offset_floor_mv), (200, -200))
        self.assertEqual(dict(p.assumed_bios_offset_mv), {"P": 0, "E": 0})
        self.assertEqual((p.max_ratio_step, p.max_v_step_mv, p.max_offset_step_mv), (8, 200, 200))
        self.assertEqual(dict(p.ratio_max), {"P": 57, "E": 49})
        self.assertEqual(dict(p.v_target_max_mv), {"P": 1625, "E": 1355})
        self.assertEqual((p.max_operations_per_session, p.max_operations_per_boot, p.min_dwell_s), (100000, 100000, 0.0))

    def test_offset_window_is_plus_minus_200_mv_for_both_kinds(self) -> None:
        p = self.policy
        top = VfWord.mv_to_offset_raw(200)
        for cpu in (0, 7, 8, 23):
            self.assertEqual(p.offset_floor_raw(cpu), -top)
        self.assertGreater(VfWord.mv_to_offset_raw(205), top)  # +-205 mV is outside the window

    def test_v_target_ceilings_are_the_hardware_maxima(self) -> None:
        self.assertEqual(dict(self.policy.v_target_max_mv), dict(SANITY_V_TARGET_MAX_MV))

    def test_sanity_ceiling_is_200_and_not_more(self) -> None:
        self.assertEqual(SANITY_OFFSET_CEILING_MV, 200)
        with self.assertRaises(PolicyError):
            load_policy("cpus = [0]\noffset_ceiling_mv = 201\n")

    def test_the_allowlist_still_denies_everything_not_listed(self) -> None:
        p = self.policy
        self.assertIsNone(p.match(0x11, 1, 0))  # domain GT slice: no entry
        self.assertIsNone(p.match(0x11, 0, 9))  # a point beyond 8 can be read, never written
        self.assertIsNone(p.match(0x17, 0, 0))  # IccMax write: never
        self.assertIsNotNone(p.match(0x11, 0, 5))
        self.assertIsNone(p.match_msr(0x651, "write"))


if __name__ == "__main__":
    unittest.main()
