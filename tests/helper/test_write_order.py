"""G-151: the restore of an accepted state that holds an OC point (the live 46x/48x E split of 2026-10-14) through the real helper loop and the REAL guard, on the simulator.

The accepted state is the reference machine's with the four E clusters split (``SimConfig.measured_split``): OC word 48x + the old 48x voltage, point 6 lowered by 15 mV, and the
seventh point the OC ratio creates (``0x30``) in ``EXPECTED-words.txt``. Cold boot (no OC point yet), warm boot (split live), and a revert to the pre-split state.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

from arltuner.controls.residual import compare_words
from arltuner.guard.policy import fused_top_ratio
from arltuner.helper.ops import _parse_expected
from arltuner.mailbox import VfWord
from arltuner.msr import SimConfig, SimMsrPort
from arltuner.msr.sim import SPLIT_E_OC_WORDS, SPLIT_E_POINT6_WORDS
from arltuner.profiles import complete_from_words
from arltuner.profiles.serialize import dump_profile
from arltuner.topology import arl_s_285k

from .test_tune import REPO, TuneCase, row, rows

EXAMPLE = REPO / "examples/reference-285k/profiles"
REF = _parse_expected((EXAMPLE / "EXPECTED-words.txt").read_text())
TOPO = arl_s_285k()
COLD = SimConfig.measured(per_point=True, reject_zero_fields=True, sticky_oc_ratio=True)
LEADS = (8, 12, 16, 20)


def warm_config() -> SimConfig:
    """A warm reboot that kept the whole accepted state: the E split of ``measured_split`` and the P-core words of the reference state."""
    cfg = SimConfig.measured_split(per_point=True, reject_zero_fields=True, sticky_oc_ratio=True)
    initial, offsets = dict(cfg.initial_vf), dict(cfg.cpu_point_offsets)
    for cpu in range(8):
        initial[cpu] = VfWord.from_u32(REF[cpu][0])
        for n, w in enumerate(REF[cpu][1:], start=1):
            offsets[(cpu, n)] = VfWord.from_u32(w).offset_raw
    return dataclasses.replace(cfg, initial_vf=initial, cpu_point_offsets=offsets)


def split_words() -> dict[int, list[int]]:
    words = {cpu: list(w) for cpu, w in REF.items()}
    for lead in LEADS:
        words[lead] = [SPLIT_E_OC_WORDS[lead], *REF[lead][1:6], SPLIT_E_POINT6_WORDS[lead], 0x30]  # ... and the seventh point the OC ratio creates: 48x, offset 0
    return words


def accepted_dir(tmp: Path, words: dict[int, list[int]], name: str = "accepted-split") -> Path:
    """An accepted state (the layout ``profiles.accept`` writes) for ``words``: one complete profile, EXPECTED-words.txt, CURRENT."""
    acc = tmp / "accepted"
    (acc / "profiles").mkdir(parents=True)
    sim = SimMsrPort(COLD)
    tops = {c.cpu: fused_top_ratio(sim.fused_word(c.cpu)) for c in TOPO.cores}
    prof = complete_from_words(TOPO, name, words, tops)  # no turbo tables: this test is about the V/F words
    (acc / "profiles" / f"{name}.toml").write_text(dump_profile(prof))
    (acc / "CURRENT").write_text(f"profile = profiles/{name}.toml\n")
    (acc / "EXPECTED-words.txt").write_text("".join(f"{cpu} " + " ".join(f"{w:X}" for w in ws) + "\n" for cpu, ws in words.items()))
    return acc


class SplitCase(TuneCase):
    def policy_steps(self, **values: int) -> None:
        text = self.policy.read_text()
        for key, value in values.items():
            text = text.replace(next(line for line in text.splitlines() if line.startswith(f"{key} =")), f"{key} = {value}")
        self.policy.write_text(text)

    def restore(self, h: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        plan = self.plan(h, {"restore": "accepted"})
        return plan, self.apply_ok(h, plan)

    def vf_log(self, cpu: int) -> list[tuple[int, int]]:
        """(P2, word) of every 0x11 sent to ``cpu``, in order."""
        return [(r.p2, r.data) for r in self.sim.requests if r.cmd == 0x11 and r.cpu == cpu]

    def assert_expected(self, words: dict[int, list[int]]) -> None:
        for lead, want in words.items():
            for cpu in TOPO.unit_cpus(TOPO.unit_of_cpu(lead)):
                self.assertEqual([f"{self.sim.vf(cpu, 0, p).to_u32():X}" for p in range(len(want))], [f"{w:X}" for w in want], f"cpu {cpu}")


class ColdBoot(SplitCase):
    def test_one_shot_restore_writes_the_oc_words_before_point_6_and_the_compare_holds_the_seventh_point(self) -> None:
        h = self.boot(COLD, accepted_dir=accepted_dir(self.tmp, split_words()))
        self.read(h)
        plan, result = self.restore(h)
        self.assertEqual(plan["rounds"], 1)
        self.assert_expected(split_words())
        for lead in LEADS:
            for cpu in TOPO.unit_cpus(TOPO.unit_of_cpu(lead)):
                log = self.vf_log(cpu)
                self.assertEqual([p2 for p2, _ in log], [0, 6], f"cpu {cpu}: the OC word, then point 6")
                oc = VfWord.from_u32(log[0][1])
                self.assertEqual(oc.ratio, 48)
                self.assertEqual(oc.v_target_raw, VfWord.from_u32(SPLIT_E_OC_WORDS[lead]).v_target_raw)  # the exact target: never zero, never vtAuto
                self.assertNotIn(7, [p2 for p2, _ in log], "the OC point's slot is never written as a fused point")
        self.assertTrue(all(r.data != 0 for r in self.sim.requests if r.cmd == 0x11 and r.p2 == 0), "no zero OC word is ever sent")
        favored = [(r.cpu, r.data) for r in self.sim.requests if r.cmd == 0x1D]
        self.assertTrue(favored and all(v == 48 for _, v in favored), favored)  # 0x1D = 48: no favored decrease
        self.assertEqual({self.sim.cpuinfo_max_khz(c) for c in range(8, 24)}, {4_900_000})
        again = self.read(h)
        self.assertTrue(all(c["match"] and not c.get("equivalent") for c in again["accepted_match"]), again["accepted_match"])
        self.assertEqual(len(again["accepted_match"]), len(split_words()))

    def test_a_small_step_limit_splits_into_rounds_and_each_round_keeps_the_order(self) -> None:
        self.policy_steps(max_offset_step_mv=10)  # point 6 needs -49.8 mV from the cold 0: five rounds
        h = self.boot(COLD, accepted_dir=accepted_dir(self.tmp, split_words()))
        self.read(h)
        plan, result = self.restore(h)
        self.assertGreater(plan["rounds"], 1)
        self.assertTrue(result["complete"])
        self.assert_expected(split_words())
        for lead in LEADS:
            for cpu in TOPO.unit_cpus(TOPO.unit_of_cpu(lead)):
                log = self.vf_log(cpu)
                self.assertEqual(log[0][0], 0, f"cpu {cpu}: the OC word is the first write")
                self.assertEqual(VfWord.from_u32(log[0][1]).ratio, 48)
                self.assertLessEqual({p2 for p2, _ in log}, {0, 6})
                self.assertIn(6, [p2 for p2, _ in log])

    def test_before_the_restore_the_missing_seventh_point_is_not_a_difference_of_its_own(self) -> None:
        h = self.boot(COLD, accepted_dir=accepted_dir(self.tmp, split_words()))
        res = self.read(h)
        ec0 = next(c for c in res["accepted_match"] if c["cpu"] == 8)
        self.assertFalse(ec0["match"])
        self.assertEqual(sorted(d["point"] for d in ec0["diffs"]), [0, 6])  # the OC word and the lowered point; no entry for point 7

    def test_compare_words_judges_the_slot_by_the_oc_word(self) -> None:
        sim = SimMsrPort(COLD)
        fused = sim.fused_word(8)
        want = split_words()[8]
        cold = [0, *want[1:6], 0xFB80002E]
        self.assertEqual(compare_words(fused, want, cold), ("bad", ""))  # differs (the OC word, point 6) ...
        self.assertEqual(compare_words(fused, want, want), ("ok", ""))
        wrong_slot = [*want[:7], 0x00200030]  # the OC word is written but the seventh point has an offset
        self.assertEqual(compare_words(fused, want, wrong_slot)[0], "bad")
        self.assertEqual(compare_words(fused, want, want[:7])[0], "bad")  # the OC word is there but the point is not: a failure


class WarmBoot(SplitCase):
    def test_the_split_live_is_restored_with_no_write(self) -> None:
        h = self.boot(warm_config(), accepted_dir=accepted_dir(self.tmp, split_words()))
        res = self.read(h)
        self.assertTrue(all(c["match"] and not c.get("equivalent") for c in res["accepted_match"]), res["accepted_match"])
        plan, result = self.restore(h)
        self.assertEqual(plan["steps"], [])
        self.assertEqual(result["writes_sent"], 0)
        self.assertFalse([r for r in self.sim.requests if r.cmd in (0x11, 0x15, 0x1D, 0x27)])

    def test_reverting_to_the_pre_split_state_raises_point_6_first_and_is_equivalent(self) -> None:
        h = self.boot(warm_config(), accepted_dir=accepted_dir(self.tmp, {cpu: list(w) for cpu, w in REF.items()}, "accepted-pre-split"))
        self.read(h)
        _, result = self.restore(h)
        self.assertTrue(result["residual"], result)
        for lead in LEADS:
            for cpu in TOPO.unit_cpus(TOPO.unit_of_cpu(lead)):
                log = self.vf_log(cpu)
                self.assertEqual([p2 for p2, _ in log], [6, 0], f"cpu {cpu}: point 6 raised first, then the OC word neutralised")
                self.assertEqual(VfWord.from_u32(log[1][1]).ratio, 48)  # the OC ratio cannot be removed: never 0
        self.assertNotIn(0x1D, [r.cmd for r in self.sim.requests])
        after = self.read(h)
        ec = [c for c in after["accepted_match"] if c["cpu"] in LEADS]
        self.assertTrue(all(c["match"] and c["equivalent"] for c in ec), ec)
        self.assertTrue(all(c["match"] for c in after["accepted_match"]), after["accepted_match"])


class Policy(SplitCase):
    def test_the_policy_is_not_widened(self) -> None:
        before = self.policy.read_text() if self.policy.exists() else ""
        h = self.boot(COLD, accepted_dir=accepted_dir(self.tmp, split_words()))
        self.read(h)
        self.restore(h)
        self.assertEqual(self.policy.read_text() if self.policy.exists() else "", before)


class PreviewOrder(SplitCase):
    """G-153: the Review dialog lists the order the apply sends (the same ``reorder_writes``), from the last read."""

    INTENT = rows(row("EC1", points={"46": -45}, v=1.115, ratio=48))

    def preview_vf(self, plan: dict[str, Any], cpu: int) -> list[int]:
        return [s["point"] for s in plan["steps"] if s["kind"] == "write_vf_point" and s["cpu"] == cpu or s["kind"] == "write_vf" and s["cpu"] == cpu]

    def test_the_split_live_lists_the_oc_word_before_point_6_and_the_apply_sends_that_order(self) -> None:
        h = self.boot(warm_config(), accepted_dir=accepted_dir(self.tmp, split_words()))
        self.read(h)
        plan = self.plan(h, self.INTENT)
        self.assertNotIn("the apply orders the writes", " ".join(w["text"] for w in plan["warnings"]))
        self.assertEqual(self.preview_vf(plan, 12), [0, 6])
        self.apply_ok(h, plan)
        self.assertEqual([p2 for p2, _ in self.vf_log(12)][:2], self.preview_vf(plan, 12))

    def test_a_pure_undervolt_keeps_the_points_first(self) -> None:
        h = self.boot(warm_config(), accepted_dir=accepted_dir(self.tmp, split_words()))
        self.read(h)
        plan = self.plan(h, rows(row("EC1", points={"46": -45})))
        self.assertEqual(self.preview_vf(plan, 12), [6])

    def test_without_a_read_the_static_order_stays_with_a_note(self) -> None:
        h = self.boot(warm_config(), accepted_dir=accepted_dir(self.tmp, split_words()))
        plan = self.plan(h, rows(row("EC1", curve=-10)))
        self.assertEqual(self.preview_vf(plan, 12), [0])
        self.assertTrue(any(w["text"].startswith("the apply orders the writes so that no ratio's voltage dips (G-151)") for w in plan["warnings"]), plan["warnings"])
