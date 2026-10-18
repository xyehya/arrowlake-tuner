"""G-137 / D-039 / D-049: the code keeps only the CPU's own limits (read live) and the field encodings; there is no policy ceiling to clamp.

The ratio limit is ``0x01 [7:0]`` (the fused max OC ratio), the V target limit ``0x28 [11:0] - 1 LSB`` (field 0 = 2.0 V); an unread CPU limit refuses a raise naming the read;
every refusal names where its bound comes from. Words: ``0x778`` = 120x with bits [10:8] set is what the reference machine answers to ``0x01 P1=0`` on a P CPU
``[MEASURED win 2026-10-01]``. Simulator/recording transport only.
"""
from __future__ import annotations

import unittest

from arltuner.guard import Policy, PolicyError, load_policy
from arltuner.guard.policy import (
    CPU_LIMIT_UNREAD,
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


def policy(text: str = "") -> Policy:
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
        self.assertIsNone(hw_v_max_mv(None))  # unread
        self.assertEqual(hw_v_max_mv(0), 2000)  # D-049: a field of 0 is 2.0 V (registers.md 0x28), the same meaning hw_v_max_raw has

    def test_the_policy_can_be_bound_to_the_limits_read_live(self) -> None:
        p = policy()
        bound = p.with_hw_limits({"P": 100}, {"P": 1625})
        self.assertEqual((dict(bound.hw_ratio_max), dict(bound.hw_v_target_max_mv)), ({"P": 100, "E": None}, {"P": 1625, "E": None}))
        self.assertEqual(dict(bound.with_hw_limits({"E": 80}).hw_ratio_max), {"P": 100, "E": 80})  # kinds not given keep their value
        self.assertEqual(dict(p.hw_ratio_max), {"P": None, "E": None})  # the original is unchanged
        self.assertEqual(bound, p.with_hw_limits({"P": 100}, {"P": 1625}))
        with self.assertRaises(PolicyError):
            p.with_hw_limits({"P": 0})  # a limit the field cannot carry


class RatioLimitIsTheCpus(unittest.TestCase):
    def test_the_context_carries_the_cpu_limit_of_the_kind(self) -> None:
        p = policy()
        for cpu in (0, 8):
            self.assertEqual(context(p, cpu, oc=[OC_120]).hw_ratio_max, 120, cpu)
            self.assertEqual(context(p, cpu, oc=[oc_word(84)]).hw_ratio_max, 84, cpu)

    def test_the_text_names_the_source_of_the_bound(self) -> None:
        text = context(policy(), 0).ratio_text
        self.assertEqual(text, "CPU limit 120x (cmd 0x01 [7:0] fused max OC ratio of a P CPU, registers.md)")
        self.assertIn("E CPU", context(policy(), 8).ratio_text)

    def test_an_unread_cpu_limit_is_unknown_and_the_text_names_the_read(self) -> None:
        p = policy()
        for oc in ((), (None,)):
            c = context(p, 0, oc=oc)
            self.assertIsNone(c.hw_ratio_max, oc)
            self.assertTrue(c.ratio_text.startswith(CPU_LIMIT_UNREAD), c.ratio_text)
            self.assertIn("cmd 0x01", c.ratio_text)
            self.assertIn("was not read", c.ratio_text)
        for oc in ((0,), (oc_word(90, 0b110),)):  # read, but the CPU says no ratio overclocking
            c = context(p, 0, oc=oc)
            self.assertIsNone(c.hw_ratio_max, oc)
            self.assertIn("not supported", c.ratio_text)
            self.assertNotIn(CPU_LIMIT_UNREAD, c.ratio_text)

    def test_the_cpu_limit_bound_to_the_policy_counts_like_a_session_read(self) -> None:
        p = policy().with_hw_limits({"P": 100})
        self.assertEqual(context(p, 0, oc=()).hw_ratio_max, 100)
        self.assertEqual(context(p, 0, oc=[oc_word(84)]).hw_ratio_max, 84)  # the lowest of the two sources

    def test_the_plain_turbo_table_follows_the_same_limit(self) -> None:
        p = policy()
        e = build_msr_context(p, 0x650, oc_cap_words={8: OC_120})
        self.assertEqual((e.hw_ratio_max, e.kind), (120, "E"))
        self.assertIn("cmd 0x01", e.ratio_text)
        self.assertEqual(build_msr_context(p, 0x1AD, oc_cap_words={0: oc_word(80)}).hw_ratio_max, 80)
        self.assertIsNone(build_msr_context(p, 0x1AD, oc_cap_words={8: OC_120}).hw_ratio_max)  # only a P CPU's word answers for the P table

    def test_ratio_limit_returns_value_and_text(self) -> None:
        self.assertEqual(ratio_limit(policy(), "P", [OC_120])[0], 120)
        self.assertIsNone(ratio_limit(policy(), "P")[0])


class VTargetLimitIsTheCpus(unittest.TestCase):
    def test_the_limit_is_the_0x28_word_with_the_source(self) -> None:
        raw, text = v_target_limit(policy(), "P", 0x681)
        self.assertEqual(raw, VfWord.v_to_raw(1.625))
        self.assertEqual(text, "CPU limit 1625 mV (cmd 0x28 [11:0] - 1 LSB of a P CPU, XTU's rule, registers.md)")
        raw, text = v_target_limit(policy(), "E", 0x56D)
        self.assertEqual(raw, 0x56C)
        self.assertIn("1355 mV", text)

    def test_unread_is_unknown(self) -> None:
        raw, text = v_target_limit(policy(), "P", None)
        self.assertIsNone(raw)
        self.assertTrue(text.startswith(CPU_LIMIT_UNREAD), text)
        self.assertIn("cmd 0x28", text)

    def test_the_binding_from_tune_read_counts_and_the_lowest_wins(self) -> None:
        p = policy().with_hw_limits(None, {"P": 1500})
        self.assertEqual(v_target_limit(p, "P", None)[0], 1500 * 1024 // 1000)  # whole mV rounded down to raw: never above the limit
        self.assertEqual(v_target_limit(p, "P", 0x681)[0], 1500 * 1024 // 1000)
        self.assertEqual(v_target_limit(p, "P", 0x500)[0], 0x4FF)


class RefusalsNameTheirSource(BoundsCase):
    """Through a guard session: the message of each refused word says whose bound it was."""

    def test_a_ratio_above_the_cpu_limit_is_refused_with_cmd_0x01_as_the_source(self) -> None:
        guard = self.open(E8, hw_ratio_max={"E": 50, "P": 50})
        self.accept(guard, E8, word(50, E_V))
        exc = self.refuse(guard, E8, word(51, E_V), "ratio 51 above the CPU limit 50x")
        self.assertIn("CPU limit 50x (cmd 0x01", exc.detail)

    def test_without_the_cpu_limit_a_raise_is_refused_until_0x01_is_read_in_the_session(self) -> None:
        guard = self.open(E8, hw_ratio_max={"E": None, "P": None})
        exc = self.refuse(guard, E8, word(49, E_V), "ratio 49 raises the ratio (now 48)")  # the current ratio is 48
        self.assertIn(CPU_LIMIT_UNREAD, exc.detail)
        self.assertIn("cmd 0x01", exc.detail)
        self.transport.state[(8, 0x01, 0, 0)] = OC_120
        guard.execute(self.read_req(8, 0x01, p1=0), self.confirm)  # one transaction, through the guard like every other read
        self.assertEqual(guard.oc_cap_words("E"), {8: OC_120})
        self.assertEqual(guard.oc_cap_words("P"), {})
        self.accept(guard, E8, word(49, E_V))  # the session's own read is the CPU limit now
        self.refuse(guard, E8, word(121, E_V), "ratio 121 above the CPU limit 120x")

    def test_a_v_target_above_the_cpu_limit_names_cmd_0x28(self) -> None:
        guard = self.open(E8, **RAISE)
        limit = 0x56C  # 0x56D - 1 LSB
        self.accept(guard, E8, word(48, limit))
        exc = self.refuse(guard, E8, word(48, limit + 1), "cmd 0x28")
        self.assertIn(f"{limit + 1:#x}", exc.detail)
        self.assertIn(f"{limit:#x}", exc.detail)

    def test_the_offset_field_is_the_only_offset_bound(self) -> None:
        guard = self.open(P1, **RAISE)
        self.accept(guard, P1, word(55, P_V, VfWord.mv_to_offset_raw(-500)))  # D-049: no floor
        self.accept(guard, P1, word(55, P_V, VfWord.mv_to_offset_raw(-1000)))
        self.refuse(guard, P1, word(55, P_V, VfWord.mv_to_offset_raw(999)), "predicted voltage")  # the whole field upward still meets the CPU's voltage limit (cmd 0x28)


if __name__ == "__main__":
    unittest.main()
