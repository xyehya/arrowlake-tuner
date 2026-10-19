"""G-239: finishing an interrupted OC ratio through the helper (simulator): cpu 0 holds the 0x11 word of a 58x OC point (the live state after the tripped apply of 2026-10-19,
``[MEASURED lnx 2026-10-19]``) but its 0x1C[7:0] is still 57, so the intent ``{P0, ratio 58}`` plans the 0x1D / 0x27 pair and no other change.

The planner still sends the (equal) 0x11 word first: the guard's ``favored_ratio_follows_vf`` takes the ratio of the 0x11 *executed this session* (docs/knowledge/registers.md, XTU
sequence), so a plan without it would be refused at apply. The 0x11 is the word already in force, so it changes nothing.
"""
from __future__ import annotations

import dataclasses

from arltuner.mailbox import VfWord
from arltuner.msr import SimConfig

from .test_tune import TuneCase, row, rows

WORD = 0x0005B43A  # 58x, adaptive 1.4258 V (0x5B4), offset 0


def interrupted() -> SimConfig:
    cfg = SimConfig.measured(per_point=True)
    initial = dict(cfg.initial_vf)
    initial[0] = VfWord.from_u32(WORD)
    return dataclasses.replace(cfg, initial_vf=initial)


class FinishOcRatio(TuneCase):
    def start_interrupted(self):
        h = self.boot(interrupted())
        self.read(h)
        self.assertEqual(self.sim.vf(0).to_u32(), WORD)
        self.assertEqual(self.sim.favored_word(0) & 0xFF, 57)
        return h

    def kinds(self, plan: dict) -> list[str]:
        return [st["kind"] for st in plan["steps"] if st["kind"].startswith("write")]

    def test_the_plan_is_the_equal_word_then_0x1d_and_0x27(self) -> None:
        h = self.start_interrupted()
        p = self.plan(h, rows(row("P0", ratio=58)))
        self.assertEqual(self.kinds(p), ["write_percore_enable", "write_vf", "write_favored", "write_granular"])
        self.assertEqual(p["units"], ["P0"])
        self.assertEqual({st["cpu"] for st in p["steps"] if "cpu" in st}, {0})
        self.assertFalse([st for st in p["steps"] if st["kind"] == "write_turbo"])  # D-050: no turbo table

    def test_apply_ends_with_58_in_0x1c_and_the_word_unchanged(self) -> None:
        h = self.start_interrupted()
        res = self.apply_ok(h, self.plan(h, rows(row("P0", ratio=58))))
        self.assertEqual(self.sim.vf(0).to_u32(), WORD)
        self.assertEqual(self.sim.favored_word(0) & 0xFF, 58)
        self.assertEqual(self.sim.favored_word(0) >> 8 & 0xFF, 57)  # the fused byte [15:8] is untouched
        self.assertTrue(all(rb["match"] for rb in res["readback"]), res["readback"])

    def test_a_late_v_target_on_the_equal_word_still_completes(self) -> None:
        """The equal 0x11 carries the OC point's V target: the G-237 re-reads cover a target that lands late on the re-write (whether hardware does that is [UNVERIFIED])."""
        cfg = dataclasses.replace(interrupted(), late_target_reads=2)
        h = self.boot(cfg)
        self.read(h)
        self.apply_ok(h, self.plan(h, rows(row("P0", ratio=58))))
        self.assertEqual(self.sim.favored_word(0) & 0xFF, 58)
        self.assertEqual(self.sim.vf(0).to_u32(), WORD)
