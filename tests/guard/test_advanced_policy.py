"""D-032 / D-036 / D-039: the shipped default policy file (the advanced-user limits, generic) loads with exactly the numbers the operator approved; the code keeps only encoding bounds."""
from __future__ import annotations

import unittest
from pathlib import Path

from arltuner.guard import PolicyError, load_policy
from arltuner.guard.policy import ENCODING_OFFSET_CEILING_MV, ENCODING_OFFSET_FLOOR_MV
from arltuner.mailbox import VfWord
from arltuner.topology import arl_s_285k, synthetic

POLICY = Path(__file__).resolve().parents[2] / "policies" / "default.toml"


class AdvancedPolicy(unittest.TestCase):
    def setUp(self) -> None:
        self.topology = arl_s_285k()
        self.policy = load_policy(POLICY, self.topology)

    def test_numbers_are_the_d032_ones(self) -> None:
        p = self.policy
        self.assertEqual((p.offset_ceiling_mv, p.effective_offset_floor_mv), (200, -200))
        self.assertEqual(dict(p.assumed_bios_offset_mv), {"P": 0, "E": 0})
        self.assertEqual((p.max_ratio_step, p.max_v_step_mv, p.max_offset_step_mv), (8, 200, 200))
        self.assertEqual((dict(p.ratio_max), p.ratio_auto), ({"P": None, "E": None}, frozenset({"P", "E"})))  # "auto": the fused 0x1C values read in the session, for both kinds (D-036)
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
            self.assertEqual(ctx.v_target_max_raw, VfWord.v_to_raw({"P": 1625, "E": 1355}[kind] / 1000))  # P 1.625 V, E 1.355 V: what these words say, not constants of the code

    def test_the_policy_is_not_tied_to_the_reference_cpu(self) -> None:
        small = load_policy(POLICY, synthetic(6, 8, favored_p=(0,), favored_ratio=55, p_ratio=53, e_ratio=44))
        self.assertEqual(small.cpus, frozenset(range(14)))
        self.assertEqual(small.e_cpus, frozenset(range(6, 14)))
        with self.assertRaises(PolicyError):
            load_policy(POLICY)  # "auto" never falls back to a default topology

    def test_the_offset_bounds_are_the_field_encoding_not_the_policy_defaults(self) -> None:
        self.assertEqual((ENCODING_OFFSET_FLOOR_MV, ENCODING_OFFSET_CEILING_MV), (-1000, 999))
        self.assertEqual(load_policy("cpus = [0]\noffset_ceiling_mv = 500\neffective_offset_floor_mv = -500\n").offset_ceiling_mv, 500)  # a policy may go past the shipped 200 mV
        with self.assertRaises(PolicyError):
            load_policy("cpus = [0]\noffset_ceiling_mv = 1000\n")

    def test_the_allowlist_still_denies_everything_not_listed(self) -> None:
        p = self.policy
        self.assertIsNone(p.match(0x11, 1, 0))  # domain GT slice: no entry
        self.assertIsNone(p.match(0x11, 0, 9))  # a point beyond 8 can be read, never written
        self.assertIsNone(p.match(0x17, 0, 0))  # IccMax write: never
        self.assertIsNotNone(p.match(0x11, 0, 5))
        self.assertIsNone(p.match_msr(0x651, "write"))


if __name__ == "__main__":
    unittest.main()
