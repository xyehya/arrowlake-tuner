"""G-154 (D-045): the shipped policy's ring entries match what the ring in the accepted state needs -- reads P2 0..10, point-offset writes P2 1..9 under
``vf_point_offset_bounded`` (the 8 fused points + the OC-created 40x slot, operator yes 2026-10-15) -- and nothing writes the ring OC word (``0x11 P1=2 P2=0``)."""
from __future__ import annotations

import unittest
from pathlib import Path

from arltuner.guard import load_policy
from arltuner.topology import arl_s_285k

SHIPPED = Path(__file__).resolve().parents[2] / "policies/default.toml"


class ShippedRingEntries(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = load_policy(SHIPPED, arl_s_285k())

    def test_the_ring_reads_cover_the_oc_word_and_points_1_to_10(self) -> None:
        for p2 in range(0, 11):
            entry = self.policy.match(0x10, 2, p2)
            self.assertIsNotNone(entry, p2)
            self.assertEqual(entry.kind, "read", p2)
        self.assertIsNone(self.policy.match(0x10, 2, 11))

    def test_the_ring_point_writes_cover_1_to_9_and_the_oc_word_is_never_written(self) -> None:
        for p2 in range(1, 10):
            entry = self.policy.match(0x11, 2, p2)
            self.assertIsNotNone(entry, p2)
            self.assertEqual((entry.kind, entry.check), ("write", "vf_point_offset_bounded"), p2)
        self.assertIsNone(self.policy.match(0x11, 2, 0), "the ring OC word is BIOS-owned: no entry writes it")
        self.assertIsNone(self.policy.match(0x11, 2, 10), "P2 = 10 answers rc 0x11: nothing to write")
        self.assertIsNone(self.policy.match(0x11, 1, 3))  # no other domain's points either


if __name__ == "__main__":
    unittest.main()
