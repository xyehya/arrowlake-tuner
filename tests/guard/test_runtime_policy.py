"""G-216 / D-049: the ``[runtime]`` table is gone from the policy (an old file still loads, the table is ignored) and the ``runtime_attempt`` audit event stays. Policy parsing only; no hardware."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from arltuner.guard import AuditLog, PolicyError, load_policy, read_state
from arltuner.guard.audit import EVENTS
from arltuner.topology import arl_s_285k

SHIPPED = Path(__file__).resolve().parents[2] / "policies/default.toml"
BASE = 'cpus = "auto"\n'


def load(extra: str):
    return load_policy(BASE + extra, arl_s_285k())


class RuntimeTableIsIgnored(unittest.TestCase):
    def test_absent_means_none(self) -> None:
        self.assertIsNone(load("").runtime)

    def test_the_shipped_policy_has_no_runtime_table_and_no_commented_example(self) -> None:
        policy = load_policy(SHIPPED, arl_s_285k())
        self.assertIsNone(policy.runtime)
        self.assertEqual(policy.ignored_keys, ())
        text = SHIPPED.read_text(encoding="utf-8")
        self.assertNotIn("\n[runtime]", text)
        self.assertNotIn("# [runtime]", text)

    def test_an_old_table_loads_is_ignored_and_named_once(self) -> None:
        with self.assertLogs("arltuner.policy", level="WARNING") as logs:
            p = load("[runtime]\ncap_ratio_min = 8\nuncore_ratio = [8, 40]\n")
        self.assertIsNone(p.runtime)
        self.assertEqual(p.ignored_keys, ("runtime.cap_ratio_min", "runtime.uncore_ratio"))
        self.assertEqual(len(logs.records), 1)
        self.assertIn("runtime.uncore_ratio", logs.records[0].getMessage())

    def test_the_old_values_are_not_validated(self) -> None:
        for bad in ("cap_ratio_min = 0", "cap_ratio_min = 256", 'cap_ratio_min = "8"', "uncore_ratio = [40, 8]", "uncore_ratio = [8, 300]", "parking = true"):
            p = load("[runtime]\n" + bad + "\n")
            self.assertEqual(len(p.ignored_keys), 1, bad)
            self.assertTrue(p.ignored_keys[0].startswith("runtime."), bad)

    def test_a_non_table_is_ignored_too_but_other_unknown_keys_are_not(self) -> None:
        self.assertEqual(load("runtime = 3\n").ignored_keys, ("runtime",))
        with self.assertRaises(PolicyError):
            load("uncore_max = 40\n")


class AuditEvent(unittest.TestCase):
    def test_runtime_attempt_is_an_event_and_does_not_count_for_the_guard(self) -> None:
        self.assertIn("runtime_attempt", EVENTS)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "audit.jsonl"
            log = AuditLog(path)
            log.append("runtime_attempt", ts=1792324800.0, operator="t", kind="runtime", boot_id="b", domain="caps", key="cpu3", written=4_500_000)
            state = read_state(path)
            self.assertEqual((state.last_attempt_wall, dict(state.attempts_by_boot)), (None, {}))


if __name__ == "__main__":
    unittest.main()
