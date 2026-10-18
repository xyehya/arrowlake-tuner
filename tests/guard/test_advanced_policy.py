"""D-032 / D-036 / D-039 / D-049: the shipped default policy file is generic and carries PROCESS settings and the allowlists only -- no operator value limit."""
from __future__ import annotations

import unittest
from pathlib import Path

from arltuner.guard import PolicyError, load_policy
from arltuner.guard.policy import ENCODING_OFFSET_CEILING_MV, ENCODING_OFFSET_FLOOR_MV, LEGACY_KEYS, build_context
from arltuner.topology import arl_s_285k, synthetic

POLICY = Path(__file__).resolve().parents[2] / "policies" / "default.toml"


class AdvancedPolicy(unittest.TestCase):
    def setUp(self) -> None:
        self.topology = arl_s_285k()
        self.policy = load_policy(POLICY, self.topology)

    def test_the_shipped_file_is_free_of_the_removed_keys(self) -> None:
        self.assertEqual(self.policy.ignored_keys, ())
        text = POLICY.read_text(encoding="utf-8")
        for key in LEGACY_KEYS:
            self.assertNotRegex(text, rf"(?m)^\s*\[?{key}\b", key)  # as a key or table; the header comment may still name them

    def test_process_settings(self) -> None:
        p = self.policy
        self.assertEqual((p.max_operations_per_session, p.max_operations_per_boot, p.min_dwell_s), (100000, 100000, 0.0))

    def test_the_offset_bound_is_the_field_encoding_only(self) -> None:
        self.assertEqual((ENCODING_OFFSET_FLOOR_MV, ENCODING_OFFSET_CEILING_MV), (-1000, 999))
        p = load_policy("cpus = [0]\noffset_ceiling_mv = 500\neffective_offset_floor_mv = -500\n")
        self.assertEqual(p.ignored_keys, ("effective_offset_floor_mv", "offset_ceiling_mv"))  # still loads, ignored (D-049)
        self.assertEqual((p.offset_ceiling_mv, p.effective_offset_floor_mv), (999, -1000))  # the neutral stand-ins

    def test_the_voltage_limit_is_the_cpus_0x28_word_whatever_its_value(self) -> None:
        for cpu, vmax, kind in ((0, 0x681, "P"), (8, 0x56D, "E")):  # the 0x28 words of the reference CPU
            ctx = build_context(self.policy, cpu, favored_word=None, fused_word=None, vf_word_sent=None, max_voltage_word=vmax)
            self.assertEqual(ctx.hw_v_max_raw, vmax - 1)
            self.assertEqual(ctx.hw_v_max_raw * 1000 // 1024, {"P": 1625, "E": 1355}[kind])  # what these words say, not constants of the code
        ctx = build_context(self.policy, 0, favored_word=None, fused_word=None, vf_word_sent=None, max_voltage_word=0x7FF)
        self.assertEqual(ctx.hw_v_max_raw, 0x7FE)  # no code constant caps it

    def test_the_policy_is_not_tied_to_the_reference_cpu(self) -> None:
        small = load_policy(POLICY, synthetic(6, 8, favored_p=(0,), favored_ratio=55, p_ratio=53, e_ratio=44))
        self.assertEqual(small.cpus, frozenset(range(14)))
        self.assertEqual(small.e_cpus, frozenset(range(6, 14)))
        with self.assertRaises(PolicyError):
            load_policy(POLICY)  # "auto" never falls back to a default topology

    def test_the_allowlist_still_denies_everything_not_listed(self) -> None:
        p = self.policy
        self.assertIsNone(p.match(0x11, 1, 0))  # domain GT slice: no entry
        self.assertIsNone(p.match(0x11, 0, 9))  # a point beyond 8 can be read, never written
        self.assertIsNone(p.match(0x17, 0, 0))  # IccMax write: never
        self.assertIsNotNone(p.match(0x11, 0, 5))
        self.assertIsNone(p.match_msr(0x651, "write"))


if __name__ == "__main__":
    unittest.main()
