"""G-137 / D-039: every limit is the user's policy; the code keeps only the CPU's own limits (read live) and the field encodings.

A ratio ceiling is the policy value clamped by ``0x01 [7:0]`` (the fused max OC ratio), a V target ceiling the policy value clamped by ``0x28 - 1 LSB``; an unread
CPU limit is "no raise" (fail closed); every refusal names where its bound comes from. Words: ``0x778`` = 120x with bits [10:8] set is what the reference
machine answers to ``0x01 P1=0`` on a P CPU ``[MEASURED win 2026-10-01]``. Simulator/recording transport only.
"""
from __future__ import annotations

import unittest

from arltuner.guard import Policy, PolicyError, Refusal, load_policy
from arltuner.guard.policy import (
    ENCODING_OFFSET_CEILING_MV,
    CheckContext,
    build_context,
    build_msr_context,
    hw_ratio_max,
    hw_v_max_mv,
    ratio_limit,
    v_target_limit,
)
from arltuner.mailbox import VfWord
from arltuner.topology import arl_s_285k

from .test_bounds import E8, E_V, P1, P_V, RAISE, BoundsCase, word

OC_120 = 0x778  # 120x, ratio OC ok, voltage override ok, offset ok
TOPOLOGY = arl_s_285k()


def oc_word(ratio: int, flags: int = 0b111) -> int:
    return ratio | flags << 8


def policy(text: str) -> Policy:
    return load_policy('cpus = "auto"\n' + text, TOPOLOGY)


def context(p: Policy, cpu: int, *, oc=(OC_120,), favored: int | None = 0x3939, vmax: int | None = None) -> CheckContext:
    return build_context(p, cpu, favored_word=favored, fused_word=None, vf_word_sent=None, max_voltage_word=vmax, oc_cap_words_of_kind=oc)


class TheCpuLimit(unittest.TestCase):
    def test_the_ratio_limit_is_byte_0_of_0x01_and_needs_the_ratio_oc_flag(self) -> None:
        self.assertEqual(hw_ratio_max([OC_120]), 120)
        self.assertEqual(hw_ratio_max([oc_word(90), oc_word(74)]), 74)  # the lowest word of the kind (fail closed)
        for words in ([], [None], [0], [oc_word(0)], [oc_word(90, 0b110)], [oc_word(90, 0)]):
            self.assertIsNone(hw_ratio_max(words), words)  # not read / reads 0 / ratio overclocking not ok

    def test_the_v_target_limit_is_0x28_minus_one_lsb_in_whole_mv(self) -> None:
        self.assertEqual((hw_v_max_mv(0x681), hw_v_max_mv(0x56D)), (1625, 1355))
        self.assertEqual(hw_v_max_mv(0x1000 | 0x681), 1625)  # only [11:0] counts
        self.assertIsNone(hw_v_max_mv(None))
        self.assertIsNone(hw_v_max_mv(0))  # a field of 0 is not "no limit" and not 2.0 V here

    def test_the_policy_can_be_bound_to_the_limits_read_live(self) -> None:
        p = policy('[ratio_max]\nP = 90\nE = 49\n')
        bound = p.with_hw_limits({"P": 100}, {"P": 1625})
        self.assertEqual((dict(bound.hw_ratio_max), dict(bound.hw_v_target_max_mv)), ({"P": 100, "E": None}, {"P": 1625, "E": None}))
        self.assertEqual(dict(bound.with_hw_limits({"E": 80}).hw_ratio_max), {"P": 100, "E": 80})  # kinds not given keep their value
        self.assertEqual(dict(p.hw_ratio_max), {"P": None, "E": None})  # the original is unchanged
        self.assertEqual(bound, p.with_hw_limits({"P": 100}, {"P": 1625}))
        with self.assertRaises(PolicyError):
            p.with_hw_limits({"P": 0})  # a limit the field cannot carry


class RatioCeilingIsThePolicyClampedByTheCpu(unittest.TestCase):
    def test_a_policy_of_90_loads_and_is_enforced_up_to_the_cpu_limit(self) -> None:
        p = policy('[ratio_max]\nP = 90\nE = 90\n')
        self.assertEqual((p.ratio_max["P"], p.ratio_max["E"]), (90, 90))  # no code constant refuses it at load
        for cpu in (0, 8):
            self.assertEqual(context(p, cpu, oc=[OC_120]).ratio_max, 90, cpu)  # the CPU allows 120: the policy value stands
            self.assertEqual(context(p, cpu, oc=[oc_word(84)]).ratio_max, 84, cpu)  # the CPU allows only 84: it wins, whatever the policy says

    def test_the_text_names_the_source_of_the_bound(self) -> None:
        p = policy('[ratio_max]\nP = 90\n')
        self.assertEqual(
            context(p, 0).ratio_text,
            "policy ratio_max P = 90 (the policy; your CPU allows up to 120x per cmd 0x01)",
        )
        clamped = context(p, 0, oc=[oc_word(84)]).ratio_text
        self.assertTrue(clamped.startswith("CPU limit 84x (cmd 0x01"), clamped)
        self.assertIn("policy ratio_max P = 90", clamped)

    def test_the_text_names_the_policy_file(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "policy.toml"
            path.write_text('cpus = "auto"\n[ratio_max]\nP = 90\n', encoding="utf-8")
            p = load_policy(path, TOPOLOGY)
            self.assertEqual(p.source, str(path))
            self.assertIn(f"({path};", context(p, 0).ratio_text)
            self.assertEqual(p, load_policy('cpus = "auto"\n[ratio_max]\nP = 90\n', TOPOLOGY))  # the file name is no part of the policy's identity

    def test_an_unread_cpu_limit_is_no_raise(self) -> None:
        p = policy('[ratio_max]\nP = 90\nE = 90\n')
        for oc in ((), (None,), (0,), (oc_word(90, 0b110),)):
            c = context(p, 0, oc=oc)
            self.assertIsNone(c.ratio_max, oc)
            self.assertTrue(c.ratio_text.startswith("no-raise value: policy ratio_max P = 90"), c.ratio_text)
            self.assertIn("cmd 0x01", c.ratio_text)
        self.assertIn("not supported", context(p, 0, oc=(oc_word(90, 0b110),)).ratio_text)  # read, but the CPU says no ratio overclocking
        self.assertIn("was not read", context(p, 0, oc=()).ratio_text)

    def test_auto_is_the_fused_value_plus_3_clamped_by_the_cpu(self) -> None:
        p = policy('[ratio_max]\nP = "auto"\n')
        self.assertEqual(context(p, 0, favored=0x3939).ratio_max, 60)
        self.assertEqual(context(p, 0, favored=0x3939, oc=[oc_word(59)]).ratio_max, 59)
        self.assertIsNone(context(p, 0, favored=0x3939, oc=()).ratio_max)
        self.assertIn('policy ratio_max P = "auto"', context(p, 0, favored=0x3939).ratio_text)

    def test_the_cpu_limit_bound_to_the_policy_counts_like_a_session_read(self) -> None:
        p = policy('[ratio_max]\nP = 90\n').with_hw_limits({"P": 100})
        self.assertEqual(context(p, 0, oc=()).ratio_max, 90)
        self.assertEqual(context(p, 0, oc=[oc_word(84)]).ratio_max, 84)  # the lowest of the two sources

    def test_the_plain_turbo_table_follows_the_same_ceiling(self) -> None:
        p = policy('[ratio_max]\nP = 90\nE = 49\n')
        e = build_msr_context(p, 0x650, platform_info=None, oc_cap_words={8: OC_120})
        self.assertEqual((e.ratio_max, e.kind), (49, "E"))
        self.assertIn("policy ratio_max E = 49", e.ratio_text)
        self.assertEqual(build_msr_context(p, 0x1AD, platform_info=None, oc_cap_words={0: oc_word(80)}).ratio_max, 80)
        self.assertIsNone(build_msr_context(p, 0x1AD, platform_info=None, oc_cap_words={8: OC_120}).ratio_max)  # only a P CPU's word answers for the P table


class VTargetCeilingIsThePolicyClampedByTheCpu(unittest.TestCase):
    def test_a_policy_above_the_cpu_is_clamped_with_the_source(self) -> None:
        p = policy('[v_target_max_mv]\nP = 1700\nE = 1300\n')
        raw, text = v_target_limit(p, "P", 0x681)
        self.assertEqual(raw, VfWord.v_to_raw(1.625))
        self.assertTrue(text.startswith("CPU limit 1625 mV (cmd 0x28 max - 1 LSB"), text)
        raw, text = v_target_limit(p, "E", 0x56D)
        self.assertEqual(raw, VfWord.v_to_raw(1.3))
        self.assertEqual(text, "policy v_target_max_mv E = 1300 mV (the policy; your CPU allows up to 1355 mV per cmd 0x28)")

    def test_auto_unread_is_no_raise_and_a_number_unread_keeps_its_value_for_the_guards_0x28_rule(self) -> None:
        auto = policy('[v_target_max_mv]\nP = "auto"\n')
        self.assertEqual(v_target_limit(auto, "P", None)[0], None)
        self.assertEqual(v_target_limit(auto, "P", 0x681)[0], 0x680)
        number = policy('[v_target_max_mv]\nP = 1500\n')
        raw, text = v_target_limit(number, "P", None)
        self.assertEqual(raw, VfWord.v_to_raw(1.5))
        self.assertIn("was not read", text)


class RefusalsNameTheirSource(BoundsCase):
    """Through a guard session: the message of each refused word says whose bound it was."""

    def test_a_ratio_above_the_cpu_limit_is_refused_with_cmd_0x01_as_the_source(self) -> None:
        guard = self.open(E8, **{**RAISE, "ratio_max": {"E": 90, "P": 58}, "hw_ratio_max": {"E": 50, "P": 50}})
        self.accept(guard, E8, word(50, E_V))
        exc = self.refuse(guard, E8, word(51, E_V), "ratio 51 above 50")
        self.assertIn("CPU limit 50x (cmd 0x01", exc.detail)
        self.assertIn("policy ratio_max E = 90", exc.detail)

    def test_a_ratio_above_the_policy_is_refused_with_the_policy_and_the_cpu_limit(self) -> None:
        guard = self.open(E8, **RAISE)
        self.accept(guard, E8, word(50, E_V))
        exc = self.refuse(guard, E8, word(51, E_V), "ratio 51 above 50")
        self.assertIn("policy ratio_max E = 50 (the policy; your CPU allows up to 120x per cmd 0x01)", exc.detail)

    def test_without_the_cpu_limit_a_raise_is_refused_until_0x01_is_read_in_the_session(self) -> None:
        guard = self.open(E8, **{**RAISE, "hw_ratio_max": {"E": None, "P": None}})
        exc = self.refuse(guard, E8, word(49, E_V), "ratio 49 above 48")  # the no-raise value: the current ratio
        self.assertIn("no-raise value: policy ratio_max E = 50", exc.detail)
        self.assertIn("cmd 0x01", exc.detail)
        self.transport.state[(8, 0x01, 0, 0)] = OC_120
        guard.execute(self.read_req(8, 0x01, p1=0), self.confirm)  # one transaction, through the guard like every other read
        self.assertEqual(guard.oc_cap_words("E"), {8: OC_120})
        self.assertEqual(guard.oc_cap_words("P"), {})
        self.accept(guard, E8, word(49, E_V))  # the session's own read is the CPU limit now
        exc = self.refuse(guard, E8, word(51, E_V), "ratio 51 above 50")
        self.assertIn("policy ratio_max E = 50", exc.detail)

    def test_a_v_target_above_the_policy_names_the_policy_and_the_cpu(self) -> None:
        guard = self.open(E8, **RAISE)
        ceiling = VfWord.v_to_raw(1.25)
        self.accept(guard, E8, word(48, ceiling))
        exc = self.refuse(guard, E8, word(48, ceiling + 1), "policy v_target_max_mv E = 1250 mV (the policy; your CPU allows up to 1355 mV per cmd 0x28)")
        self.assertIn(f"{ceiling + 1:#x} above {ceiling:#x}", exc.detail)

    def test_an_offset_outside_the_window_names_the_policy_and_the_encoding(self) -> None:
        guard = self.open(E8, **RAISE)
        exc = self.refuse(guard, E8, word(48, E_V, 27), "outside")
        self.assertIn("both are policy values, the policy, within the field encoding -1024..1023", exc.detail)

    def test_a_policy_may_go_to_the_encoding_limits_of_the_offset(self) -> None:
        guard = self.open(P1, ratio_max={"E": 50, "P": 58}, offset_ceiling_mv=ENCODING_OFFSET_CEILING_MV, effective_offset_floor_mv=-500,
                          steps=dict(max_ratio_step=8, max_v_step_mv=200, max_offset_step_mv=1000), assumed_bios_offset_mv={"P": 0, "E": 0})
        self.accept(guard, P1, word(55, P_V, VfWord.mv_to_offset_raw(-500)))  # -500 mV: far past the old +-200 mV code limit, inside what the policy states
        self.refuse(guard, P1, word(55, P_V, VfWord.mv_to_offset_raw(-501)), "outside")  # ... and not a step past the policy's own floor
        self.assertEqual(Refusal.CHECK_FAILED.value, "CHECK_FAILED")


if __name__ == "__main__":
    unittest.main()
