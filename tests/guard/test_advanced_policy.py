"""D-032: the advanced-user policy file loads with exactly the numbers the operator approved, and the code sanity ceilings allow them (no more)."""
from __future__ import annotations

import unittest
from pathlib import Path

from arltuner.guard import PolicyError, load_policy
from arltuner.guard.policy import SANITY_OFFSET_CEILING_MV, SANITY_V_TARGET_MAX_MV
from arltuner.mailbox import VfWord
from arltuner.topology import arl_s_285k, synthetic

POLICY = Path(__file__).resolve().parents[2] / "policies" / "advanced-2026-10-11.toml"


class AdvancedPolicy(unittest.TestCase):
    def setUp(self) -> None:
        self.topology = arl_s_285k()
        self.policy = load_policy(POLICY, self.topology)

    def test_numbers_are_the_d032_ones(self) -> None:
        p = self.policy
        self.assertEqual((p.offset_ceiling_mv, p.effective_offset_floor_mv), (200, -200))
        self.assertEqual(dict(p.assumed_bios_offset_mv), {"P": 0, "E": 0})
        self.assertEqual((p.max_ratio_step, p.max_v_step_mv, p.max_offset_step_mv), (8, 200, 200))
        self.assertEqual(dict(p.ratio_max), {"P": 57, "E": 49})  # "auto": the live cpuinfo_max_freq ratios (the D-032 numbers on the reference CPU)
        self.assertEqual(p.v_target_auto, frozenset({"P", "E"}))  # "auto": the CPU's own 0x28 maximum minus one LSB, judged by the guard
        self.assertEqual((p.max_operations_per_session, p.max_operations_per_boot, p.min_dwell_s), (100000, 100000, 0.0))

    def test_offset_window_is_plus_minus_200_mv_for_both_kinds(self) -> None:
        p = self.policy
        top = VfWord.mv_to_offset_raw(200)
        for cpu in (0, 7, 8, 23):
            self.assertEqual(p.offset_floor_raw(cpu), -top)
        self.assertGreater(VfWord.mv_to_offset_raw(205), top)  # +-205 mV is outside the window

    def test_v_target_ceilings_are_the_hardware_maxima(self) -> None:
        from arltuner.guard.policy import build_context

        for cpu, vmax, kind in ((0, 0x681, "P"), (8, 0x56D, "E")):  # the 0x28 words of the reference CPU
            ctx = build_context(self.policy, cpu, favored_word=None, fused_word=None, vf_word_sent=None, max_voltage_word=vmax)
            self.assertEqual(ctx.v_target_max_raw, vmax - 1)
            self.assertEqual(ctx.v_target_max_raw, VfWord.v_to_raw(SANITY_V_TARGET_MAX_MV[kind] / 1000))

    def test_the_policy_is_not_tied_to_the_reference_cpu(self) -> None:
        small = load_policy(POLICY, synthetic(6, 8, favored_p=(0,), favored_ratio=55, p_ratio=53, e_ratio=44))
        self.assertEqual(small.cpus, frozenset(range(14)))
        self.assertEqual(small.e_cpus, frozenset(range(6, 14)))
        self.assertEqual(dict(small.ratio_max), {"P": 55, "E": 44})
        with self.assertRaises(PolicyError):
            load_policy(POLICY)  # "auto" never falls back to a default topology

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
