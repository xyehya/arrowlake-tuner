"""G-151: every plan writes in a transition-safe order -- no ratio's voltage dips below ``min(before, after)`` at any prefix of the sequence.

The split E curve of 2026-10-14 [MEASURED lnx] is the fixture: an OC point at 48x holding the old 48x voltage (EC0 ``0x00047E30`` = 1.1230 V, EC1-3 ``0x00047330``
= 1.1123 V) and point 6 lowered by 15 mV (EC0 ``0xF9A0002E``, EC1-3 ``0xFAE0002E``); the pre-split state is point 6 at -35 / -25 mV and no OC word. Pure planner
tests on the simulator's words; nothing is sent.
"""
from __future__ import annotations

import dataclasses
import unittest
from unittest import mock
from collections.abc import Mapping, Sequence
from pathlib import Path

from arltuner.controls import CoreReadings, PlanStep, StepKind, build_plan
from arltuner.controls.residual import neutralise
from arltuner.controls import order
from arltuner.controls.order import level
from arltuner.guard import load_policy
from arltuner.guard.policy import fused_top_ratio
from arltuner.mailbox import VfWord
from arltuner.msr import SimConfig, SimMsrPort
from arltuner.profiles import complete_from_words
from arltuner.profiles.migrate import parse_expected
from arltuner.topology import arl_s_285k

REPO = Path(__file__).resolve().parents[2]
TOPO = arl_s_285k()
EC0, ECS = (8, 9, 10, 11), ((12, 13, 14, 15), (16, 17, 18, 19), (20, 21, 22, 23))
REF = parse_expected((REPO / "examples/reference-285k/profiles/EXPECTED-words.txt").read_text())  # the pre-split accepted state: point 6 = -35 / -25 mV, no OC word
P6 = {"EC0": 0xF9A0002E, "ECS": 0xFAE0002E}  # the split: point 6 lowered by 15 mV
POINTS = [0x08, 0x0F, 0x17, 0x1E, 0x28]  # E points 1-5, offset 0 (the reference state)


def split_words() -> dict[int, list[int]]:
    """EXPECTED words of the split state: the OC word, points 1-6 and the seventh point the OC ratio creates (``0x30`` = 48x, offset 0)."""
    words = {cpu: list(w) for cpu, w in REF.items()}
    words[8] = [0x00047E30, *POINTS, P6["EC0"], 0x30]
    for cpu in (12, 16, 20):
        words[cpu] = [0x00047330, *POINTS, P6["ECS"], 0x30]
    return words


def config_for(words: Mapping[int, Sequence[int]] | None) -> SimConfig:
    """``SimConfig.measured`` at cold-boot stock, or holding ``words`` (cpu -> ``[OC word, point 1, ...]``, the extra point is the simulator's own)."""
    cfg = SimConfig.measured(per_point=True, reject_zero_fields=True)
    if words is None:
        return cfg
    initial, offsets = dict(cfg.initial_vf), dict(cfg.cpu_point_offsets)
    for lead, ws in words.items():
        for cpu in TOPO.unit_cpus(TOPO.unit_of_cpu(lead)):
            initial[cpu] = VfWord.from_u32(ws[0])
            for n, w in enumerate(ws[1:7 if TOPO.by_cpu(cpu).kind == "E" else 9], start=1):
                offsets[(cpu, n)] = VfWord.from_u32(w).offset_raw
    return dataclasses.replace(cfg, initial_vf=initial, cpu_point_offsets=offsets)


def readings(sim: SimMsrPort) -> tuple[dict[int, CoreReadings], dict[tuple[int, int], int]]:
    base = {c.cpu: CoreReadings(vf=sim.vf(c.cpu), favored=sim.favored_word(c.cpu), fused=sim.fused_word(c.cpu), granular=sim.granular_word(c.cpu)) for c in TOPO.cores}
    points = {(c.cpu, n): sim.vf(c.cpu, 0, n).to_u32() for c in TOPO.cores for n in range(1, 7 if c.kind == "E" else 9)}
    return base, points


def policy(**changes: int):
    pol = load_policy(REPO / "policies" / "default.toml", topology=TOPO)
    return dataclasses.replace(pol, **changes) if changes else pol


def profile_of(words: Mapping[int, Sequence[int]]):
    sim = SimMsrPort(config_for(None))
    return complete_from_words(TOPO, "accepted", words, {c.cpu: fused_top_ratio(sim.fused_word(c.cpu)) for c in TOPO.cores})


def plan_for(profile, held: Mapping[int, Sequence[int]] | None, **kwargs):
    base, points = readings(SimMsrPort(config_for(held)))
    return build_plan(profile, TOPO, base, per_core_enabled=True, policy=policy(), point_words=points, only_changed=profile.complete, **kwargs), base, points


def writes(plan, cpu: int) -> list[PlanStep]:
    return [s for s in plan.steps if s.kind.is_write and s.cpu == cpu and s.kind in (StepKind.WRITE_VF, StepKind.WRITE_VF_POINT)]


def check_rule(test: unittest.TestCase, plan, base: Mapping[int, CoreReadings], points: Mapping[tuple[int, int], int], cpus: Sequence[int]) -> None:
    """The rule, evaluated on the words the plan SENDS (``step.data``), not on the planner's own bookkeeping: for each CPU and each prefix of its writes, every ratio's
    voltage is at or above ``min(before, after)``."""
    for cpu in cpus:
        top = fused_top_ratio(base[cpu].fused)
        fused_raw = (base[cpu].fused >> 8) & 0xFFF
        offsets = {VfWord.from_u32(w).ratio: VfWord.from_u32(w).offset_raw for (c, _), w in points.items() if c == cpu}
        oc = base[cpu].vf
        sent = writes(plan, cpu)
        states = [(dict(offsets), oc)]
        for st in sent:
            word = VfWord.from_u32(st.data)
            offsets, oc = dict(offsets), oc
            if st.kind is StepKind.WRITE_VF_POINT:
                offsets[word.ratio] = word.offset_raw
            else:
                oc = word
            states.append((offsets, oc))
        ratios = range(min(offsets), max([top, oc.ratio, *offsets]) + 1)
        for r in ratios:
            lo = min(level(states[0][0], states[0][1], r, top, fused_raw), level(states[-1][0], states[-1][1], r, top, fused_raw))
            for k, (o, w) in enumerate(states):
                test.assertGreaterEqual(level(o, w, r, top, fused_raw), lo, f"cpu {cpu}: {r}x dips after write {k} of {[hex(s.data) for s in sent]}")


class Splits(unittest.TestCase):
    def test_the_split_state_is_what_the_rule_is_written_for(self) -> None:
        """The OLD order (every point word, then the OC word) dips 48x on EC0; this is the window the live sitting avoided by hand."""
        base, points = readings(SimMsrPort(config_for(None)))
        top, fused_raw = fused_top_ratio(base[8].fused), (base[8].fused >> 8) & 0xFFF
        p6 = VfWord.from_u32(P6["EC0"]).offset_raw
        oc = VfWord.from_u32(0x00047E30)
        before = level({46: 0}, base[8].vf, 48, top, fused_raw)
        after = level({46: p6}, oc, 48, top, fused_raw)
        dip = level({46: p6}, base[8].vf, 48, top, fused_raw)  # point 6 lowered, no OC point yet
        self.assertLess(dip, min(before, after))
        self.assertGreaterEqual(level({46: 0}, oc, 48, top, fused_raw), min(before, after))  # OC word first: held up

    def test_a_cold_boot_restore_writes_the_oc_word_before_point_6(self) -> None:
        plan, base, points = plan_for(profile_of(split_words()), None)
        self.assertTrue(plan.allowed, plan.refusals)
        for cpu in (8, 12, 16, 20):
            sent = writes(plan, cpu)
            self.assertEqual([(s.kind, s.p2) for s in sent[:1]], [(StepKind.WRITE_VF, 0)], f"cpu {cpu}")
            oc = VfWord.from_u32(sent[0].data)
            self.assertEqual(oc.ratio, 48)
            self.assertEqual(oc.v_target_raw, VfWord.from_u32(split_words()[cpu][0]).v_target_raw)  # the exact target, never 0 and never vtAuto
            self.assertEqual(sorted(s.p2 for s in sent[1:]), [6])
        check_rule(self, plan, base, points, [8, 9, 12, 13, 16, 20, 23])

    def test_the_0x1d_follows_the_oc_word_and_names_48(self) -> None:
        plan, *_ = plan_for(profile_of(split_words()), None)
        favored = [s for s in plan.steps if s.kind is StepKind.WRITE_FAVORED and s.cpu in (8, 12)]
        self.assertTrue(favored)
        self.assertEqual({s.data for s in favored}, {48})
        for s in favored:
            seq = [x for x in plan.steps if x.cpu == s.cpu and x.kind.is_write]
            self.assertEqual(seq[seq.index(s) - 1].kind, StepKind.WRITE_VF)  # 0x1D stays with its 0x11 word
        self.assertTrue(all(s.data != 0 for s in plan.steps if s.kind is StepKind.WRITE_VF))

    def test_a_warm_boot_with_the_split_live_writes_nothing(self) -> None:
        plan, *_ = plan_for(profile_of(split_words()), split_words())
        self.assertEqual([s for s in plan.steps if s.kind.is_write], [])

    def test_reverting_to_the_pre_split_state_raises_point_6_first(self) -> None:
        """Accepted = the pre-split state (no OC word), live = the split: the OC point cannot be removed, so it is neutralised (G-147) -- after point 6 is raised."""
        sim = SimMsrPort(config_for(split_words()))
        base, points = readings(sim)
        pre = profile_of(REF)
        res = neutralise(pre, TOPO, policy(), {c: base[c].vf.to_u32() for c in base}, {c: base[c].fused for c in base})
        self.assertEqual(sorted(u.unit for u in res.units), ["EC0", "EC1", "EC2", "EC3"])
        plan = build_plan(res.profile, TOPO, base, per_core_enabled=True, policy=policy(), point_words=points, only_changed=True, residual_cpus=res.cpus)
        self.assertTrue(plan.allowed, plan.refusals)
        for cpu in (8, 12, 16, 20):
            self.assertEqual([(s.kind, s.p2) for s in writes(plan, cpu)], [(StepKind.WRITE_VF_POINT, 6), (StepKind.WRITE_VF, 0)], f"cpu {cpu}")
        check_rule(self, plan, base, points, [8, 9, 12, 16, 20])

    def test_the_same_intent_as_one_gui_plan_writes_the_oc_word_first(self) -> None:
        """The pre-split state plus the split asked together (OC 48x at the old voltage, point 6 -15 mV): OC word first, then the point."""
        text = (
            "[meta]\nname = \"split\"\nplatform = \"arl-s-285k\"\n"
            "\n[[core]]\nkind = \"E\"\nindex = 4\nratio = 48\noc_voltage_mv = 1123\n"
            "\n[[point]]\nkind = \"E\"\nindex = 4\npoint = 6\noffset_mv = -50\nexpect_ratio = 46\n"
        )
        from arltuner.profiles import load_profile

        plan, base, points = plan_for(load_profile(text), REF)
        self.assertTrue(plan.allowed, plan.refusals)
        for cpu in (12, 13, 14, 15):
            self.assertEqual([(s.kind, s.p2) for s in writes(plan, cpu)], [(StepKind.WRITE_VF, 0), (StepKind.WRITE_VF_POINT, 6)], f"cpu {cpu}")
        check_rule(self, plan, base, points, [12, 15])

    def test_a_pure_undervolt_keeps_the_planners_order(self) -> None:
        pre = [
            f'\n[[point]]\nkind = "P"\nindex = 4\npoint = {n}\noffset_mv = {mv}\nexpect_ratio = {ratio}\n'
            for n, mv, ratio in ((3, -10, 30), (5, -30, 53), (6, -40, 55))
        ]
        from arltuner.profiles import load_profile

        prof = load_profile('[meta]\nname = "uv"\nplatform = "arl-s-285k"\n[[core]]\nkind = "P"\nindex = 4\noffset_mv = -20\n' + "".join(pre))
        plan, base, points = plan_for(prof, None)
        order = [(s.kind, s.p2) for s in plan.steps if s.kind in (StepKind.WRITE_VF_POINT, StepKind.WRITE_VF)]
        self.assertEqual(order, [(StepKind.WRITE_VF_POINT, 3), (StepKind.WRITE_VF_POINT, 5), (StepKind.WRITE_VF_POINT, 6), (StepKind.WRITE_VF, 0)])
        check_rule(self, plan, base, points, [4])

    def test_a_unit_with_no_safe_order_is_refused_by_name_and_nothing_is_reordered(self) -> None:
        with mock.patch.object(order, "_SEARCH_LIMIT", 0):  # the search finds no order
            plan, *_ = plan_for(profile_of(split_words()), None)
        self.assertFalse(plan.allowed)
        refused = [s for s in plan.steps if not s.verdict.allowed]
        self.assertTrue(refused and all(order.CHECK_NAME in s.verdict.detail and s.verdict.reason == "CHECK_FAILED" for s in refused), [s.verdict for s in refused])
        self.assertTrue(any("write_order" in w for w in plan.warnings))
        self.assertIsNone(plan.next_profile_text)

    def test_the_oc_points_slot_is_never_written_as_a_fused_point(self) -> None:
        from arltuner.profiles import load_profile

        prof = load_profile(
            '[meta]\nname = "slot"\nplatform = "arl-s-285k"\n[[point]]\nkind = "E"\nindex = 4\npoint = 7\noffset_mv = -5\nexpect_ratio = 48\n'
        )
        base, points = readings(SimMsrPort(config_for(split_words())))
        points.update({(c, 7): SimMsrPort(config_for(split_words())).vf(c, 0, 7).to_u32() for c in (12, 13, 14, 15)})
        plan = build_plan(prof, TOPO, base, per_core_enabled=True, policy=policy(), point_words=points)
        self.assertFalse(plan.allowed)
        self.assertIn("oc_point_slot", [s for s in plan.steps if not s.verdict.allowed][0].verdict.detail)
        only_ec3 = {20: base[20]}  # no 0x09 read of the point's own CPU: the top fused ratio of its core kind is the same
        plan = build_plan(prof, TOPO, only_ec3, per_core_enabled=True, policy=policy(), point_words=points)
        self.assertIn("oc_point_slot", [s for s in plan.steps if not s.verdict.allowed][0].verdict.detail)


if __name__ == "__main__":
    unittest.main()
