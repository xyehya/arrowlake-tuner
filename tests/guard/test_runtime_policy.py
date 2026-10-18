"""G-216: the ``[runtime]`` table of the policy (caps floor, uncore ratio range, parking) and the ``runtime_attempt`` audit event. Policy parsing only; no hardware."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from arltuner.guard import AuditLog, PolicyError, load_policy, read_state
from arltuner.guard.audit import EVENTS
from arltuner.guard.policy import Policy, RuntimePolicy
from arltuner.topology import arl_s_285k

SHIPPED = Path(__file__).resolve().parents[2] / "policies/default.toml"
BASE = 'cpus = "auto"\n'


def load(extra: str):
    return load_policy(BASE + extra, arl_s_285k())


class RuntimeTable(unittest.TestCase):
    def test_absent_means_none(self) -> None:
        self.assertIsNone(load("").runtime)

    def test_the_shipped_policy_has_no_runtime_table(self) -> None:
        """The keys are the operator's; the shipped file keeps them commented out."""
        self.assertIsNone(load_policy(SHIPPED, arl_s_285k()).runtime)
        text = SHIPPED.read_text(encoding="utf-8")
        self.assertIn("# [runtime]", text)
        self.assertNotIn("\n[runtime]", text)

    def test_a_full_table(self) -> None:
        r = load("[runtime]\ncap_ratio_min = 8\nuncore_ratio = [8, 40]\nparking = true\n").runtime
        self.assertEqual((r.cap_ratio_min, r.uncore_ratio, r.parking), (8, (8, 40), True))

    def test_every_key_is_optional_and_an_empty_table_allows_nothing(self) -> None:
        self.assertEqual(load("[runtime]\n").runtime, RuntimePolicy())
        r = load("[runtime]\nparking = false\n").runtime
        self.assertEqual((r.cap_ratio_min, r.uncore_ratio, r.parking), (None, None, False))

    def test_bad_values_are_load_errors(self) -> None:
        for bad in (
            "cap_ratio_min = 0", "cap_ratio_min = 256", "cap_ratio_min = 4.5", 'cap_ratio_min = "8"', "cap_ratio_min = true",
            "uncore_ratio = [8]", "uncore_ratio = [40, 8]", "uncore_ratio = [0, 40]", "uncore_ratio = [8, 300]", "uncore_ratio = [8.5, 40]", 'uncore_ratio = "8-40"',
            "parking = 1", 'parking = "yes"',
        ):
            with self.assertRaises(PolicyError, msg=bad):
                load("[runtime]\n" + bad + "\n")

    def test_unknown_keys_and_a_non_table_are_load_errors(self) -> None:
        with self.assertRaises(PolicyError):
            load("[runtime]\nuncore_max = 40\n")
        with self.assertRaises(PolicyError):
            load("runtime = 3\n")

    def test_the_policy_object_checks_the_type(self) -> None:
        with self.assertRaises(PolicyError):
            Policy(cpus=frozenset({0}), runtime={"parking": True})  # type: ignore[arg-type]


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
