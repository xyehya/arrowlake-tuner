"""G-214: the ``[power]`` table of the policy and the plain-MSR read allowlist for ``0x606`` / ``0x610`` / ``0x614``. Policy parsing only; no hardware."""
from __future__ import annotations

import unittest
from pathlib import Path

from arltuner.guard import MsrAllowEntry, PolicyError, default_m1_policy, load_policy
from arltuner.guard.policy import PLAIN_MSR_READABLE, PLAIN_MSR_WRITABLE, PowerPolicy
from arltuner.topology import arl_s_285k

SHIPPED = Path(__file__).resolve().parents[2] / "policies/default.toml"
BASE = 'cpus = "auto"\n'


def load(extra: str):
    return load_policy(BASE + extra, arl_s_285k())


class PowerTable(unittest.TestCase):
    def test_absent_means_none(self) -> None:
        self.assertIsNone(load("").power)

    def test_the_shipped_policy_has_no_power_table(self) -> None:
        """D-039: the bounds are the operator's; the shipped file keeps them commented out."""
        policy = load_policy(SHIPPED, arl_s_285k())
        self.assertIsNone(policy.power)
        text = SHIPPED.read_text(encoding="utf-8")
        self.assertIn("# [power]", text)
        self.assertNotIn("\n[power]", text)

    def test_a_full_table(self) -> None:
        p = load("[power]\npl1_w = [100, 253]\npl2_w = [100, 295]\ntau_s = [1, 128.5]\nmax_step_w = 50\n").power
        self.assertEqual((p.pl1_w, p.pl2_w, p.tau_s, p.max_step_w), ((100, 253), (100, 295), (1.0, 128.5), 50))

    def test_every_key_is_optional(self) -> None:
        p = load("[power]\npl1_w = [100, 200]\n").power
        self.assertEqual(p.pl1_w, (100, 200))
        self.assertIsNone(p.pl2_w)
        self.assertIsNone(p.tau_s)
        self.assertIsNone(p.max_step_w)

    def test_an_empty_table_is_a_table_with_nothing_allowed(self) -> None:
        self.assertEqual(load("[power]\n").power, PowerPolicy())

    def test_bad_tables_are_load_errors(self) -> None:
        for text, needle in (
            ("[power]\npl3_w = [1, 2]\n", "pl3_w"),  # unknown key
            ("[power]\npl1_w = 200\n", "[min, max]"),
            ("[power]\npl1_w = [200, 100]\n", "pl1_w"),  # min > max
            ("[power]\npl1_w = [0, 100]\n", "pl1_w"),  # below the field
            ("[power]\npl1_w = [100, 5000]\n", "pl1_w"),  # above the 15-bit field
            ("[power]\npl1_w = [100.5, 200]\n", "whole watts"),
            ("[power]\ntau_s = [0, 10]\n", "tau_s"),
            ("[power]\ntau_s = [1, 1e12]\n", "tau_s"),
            ("[power]\nmax_step_w = 0\n", "max_step_w"),
            ("[power]\nmax_step_w = true\n", "max_step_w"),
            ('power = "yes"\n', "power"),
        ):
            with self.subTest(text=text), self.assertRaises(PolicyError) as ctx:
                load(text)
            self.assertIn(needle, str(ctx.exception))


class PowerMsrAllowlist(unittest.TestCase):
    def test_the_three_msrs_are_readable_only(self) -> None:
        for msr in (0x606, 0x610, 0x614):
            self.assertIn(msr, PLAIN_MSR_READABLE)
            self.assertNotIn(msr, PLAIN_MSR_WRITABLE)
            MsrAllowEntry(f"r{msr:x}", msr, "read")

    def test_a_write_entry_for_them_is_a_load_error(self) -> None:
        for msr in (0x606, 0x610, 0x614):
            with self.subTest(msr=hex(msr)), self.assertRaises(PolicyError):
                MsrAllowEntry(f"w{msr:x}", msr, "write", check="turbo_ratio_bounded")
            with self.subTest(toml=hex(msr)), self.assertRaises(PolicyError):
                load(f'[[msr_allow]]\nname = "w"\nmsr = {msr:#x}\nkind = "write"\ncheck = "turbo_ratio_bounded"\n')

    def test_the_shipped_policy_reads_them(self) -> None:
        policy = load_policy(SHIPPED, arl_s_285k())
        for msr in (0x606, 0x610, 0x614):
            self.assertIsNotNone(policy.match_msr(msr, "read"), hex(msr))
            self.assertIsNone(policy.match_msr(msr, "write"), hex(msr))

    def test_the_builtin_policy_reads_them_too(self) -> None:
        policy = default_m1_policy(arl_s_285k())
        for msr in (0x606, 0x610, 0x614):
            self.assertIsNotNone(policy.match_msr(msr, "read"), hex(msr))
            self.assertIsNone(policy.match_msr(msr, "write"), hex(msr))
        self.assertIsNone(policy.power)


if __name__ == "__main__":
    unittest.main()
