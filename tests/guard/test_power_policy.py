"""G-214 / D-049: the ``[power]`` table is gone from the policy (an old file still loads, the table is ignored), and the plain-MSR read allowlist for ``0x606`` / ``0x610`` / ``0x614``
stays. Policy parsing only; no hardware."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from arltuner.guard import AuditLog, MsrAllowEntry, PolicyError, default_m1_policy, load_policy, read_state
from arltuner.guard.audit import EVENTS
from arltuner.guard.policy import PLAIN_MSR_READABLE, PLAIN_MSR_WRITABLE
from arltuner.topology import arl_s_285k

SHIPPED = Path(__file__).resolve().parents[2] / "policies/default.toml"
BASE = 'cpus = "auto"\n'


def load(extra: str):
    return load_policy(BASE + extra, arl_s_285k())


class PowerTableIsIgnored(unittest.TestCase):
    def test_absent_means_none(self) -> None:
        self.assertIsNone(load("").power)

    def test_the_shipped_policy_has_no_power_table_and_no_commented_example(self) -> None:
        policy = load_policy(SHIPPED, arl_s_285k())
        self.assertIsNone(policy.power)
        self.assertEqual(policy.ignored_keys, ())
        text = SHIPPED.read_text(encoding="utf-8")
        self.assertNotIn("\n[power]", text)
        self.assertNotIn("# [power]", text)

    def test_an_old_table_loads_is_ignored_and_named(self) -> None:
        with self.assertLogs("arltuner.policy", level="WARNING") as logs:
            p = load("[power]\npl1_w = [100, 253]\npl2_w = [100, 295]\ntau_s = [1, 128.5]\nmax_step_w = 50\n")
        self.assertIsNone(p.power)  # the neutral stand-in: PL writes need no policy
        self.assertEqual(p.ignored_keys, ("power.max_step_w", "power.pl1_w", "power.pl2_w", "power.tau_s"))
        self.assertEqual(len(logs.records), 1)
        for key in p.ignored_keys:
            self.assertIn(key, logs.records[0].getMessage())

    def test_the_old_values_are_not_validated_but_an_unknown_key_still_fails_closed(self) -> None:
        p = load("[power]\npl1_w = 200\ntau_s = [9, 1]\nmax_step_w = -3\n")
        self.assertEqual(p.ignored_keys, ("power.max_step_w", "power.pl1_w", "power.tau_s"))
        with self.assertRaises(PolicyError):
            load("[power]\npl1_w = 200\nnonsense = true\n")

    def test_an_empty_table_and_a_non_table_are_ignored_too(self) -> None:
        self.assertEqual(load("[power]\n").ignored_keys, ("power",))
        self.assertEqual(load('power = "yes"\n').ignored_keys, ("power",))


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


class PowercapAudit(unittest.TestCase):
    def test_powercap_attempt_is_an_event_but_not_a_mailbox_attempt(self) -> None:
        """A powercap file write is audited before it is sent, yet it neither counts toward the per-boot mailbox budget nor starts a dwell."""
        self.assertIn("powercap_attempt", EVENTS)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "audit.jsonl"
            log = AuditLog(path)
            log.append("powercap_attempt", kind="powercap", boot_id="boot-1", key="pl1_w", written=200_000_000)
            state = read_state(path)
            self.assertIsNone(state.last_attempt_wall)
            self.assertEqual(dict(state.attempts_by_boot), {})
            log.append("attempt", boot_id="boot-1")
            log.append("powercap_attempt", kind="powercap", boot_id="boot-1", key="pl2_w", written=250_000_000)
            state = read_state(path)
            self.assertEqual(dict(state.attempts_by_boot), {"boot-1": 1})


if __name__ == "__main__":
    unittest.main()
