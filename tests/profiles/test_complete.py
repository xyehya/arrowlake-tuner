"""G-148 (D-044): the complete-profile schema, the builder from words, the overlay of edits, and the planner's "only what differs" and rounds. Pure: no hardware."""
from __future__ import annotations

import dataclasses
import unittest
from pathlib import Path

from arltuner.controls import CoreReadings, StepKind, build_plan, ramp_rounds
from arltuner.controls.planner import MAX_RAMP_ROUNDS, check_rounds
from arltuner.controls.model import NeedsRounds
from arltuner.guard import default_m1_policy
from arltuner.guard.policy import fused_top_ratio
from arltuner.mailbox import VfWord
from arltuner.msr import SimConfig, SimMsrPort
from arltuner.profiles import BuildError, CoreSetting, PointSetting, Profile, ProfileError, TurboTables, complete_from_words, load_profile, overlay
from arltuner.profiles.migrate import parse_expected
from arltuner.profiles.serialize import dump_profile
from arltuner.topology import arl_s_285k

REPO = Path(__file__).resolve().parents[2]
EXPECTED = parse_expected((REPO / "examples/reference-285k/profiles/EXPECTED-words.txt").read_text())
TOPO = arl_s_285k()
SIM = SimMsrPort(SimConfig.measured(per_point=True))
TOPS = {c.cpu: fused_top_ratio(SIM.fused_word(c.cpu)) for c in TOPO.cores}
TABLES = {"e": [48] * 8, "p": [57, 57, 55, 55, 55, 55, 55, 55]}


def reference(tables: dict[str, list[int]] | None = TABLES) -> Profile:
    return complete_from_words(TOPO, "ref", EXPECTED, TOPS, tables)


class Builder(unittest.TestCase):
    def test_every_unit_and_fused_point_with_explicit_zeros(self) -> None:
        prof = reference()
        self.assertTrue(prof.complete)
        prof.check_complete(TOPO)
        self.assertEqual(len(prof.entries), 12)
        self.assertEqual(len(prof.points), 8 * 8 + 4 * 6)
        self.assertEqual({e.offset_mv for e in prof.entries}, {0})
        self.assertEqual({(e.ratio, e.oc_voltage_mv) for e in prof.entries}, {(None, None)})  # CPU 0's 0x39 is ratio 57 = the top fused ratio: no OC point
        self.assertEqual([p.expect_ratio for p in prof.points if (p.kind, p.index) == ("E", 0)], [8, 15, 23, 30, 40, 46])
        self.assertEqual(prof.turbo, TurboTables(e=tuple(TABLES["e"]), p=tuple(TABLES["p"])))

    def test_round_trip_through_toml(self) -> None:
        prof = reference()
        self.assertEqual(load_profile(dump_profile(prof, "header")), prof)

    def test_an_oc_point_is_ratio_and_voltage(self) -> None:
        words = {c: list(w) for c, w in EXPECTED.items()}
        words[3][0] = VfWord(60, VfWord.v_to_raw(1.4), False, 0).to_u32()
        words[3].append(VfWord(60, 0, False, 0).to_u32())  # the point the OC ratio adds: not a fused point
        prof = complete_from_words(TOPO, "oc", words, TOPS)
        p3 = next(e for e in prof.entries if (e.kind, e.index) == ("P", TOPO.by_cpu(3).index))
        self.assertEqual((p3.ratio, p3.oc_voltage_mv, p3.fused_top_ratio), (60, 1400, 57))
        self.assertEqual(len([p for p in prof.points if (p.kind, p.index) == ("P", p3.index)]), 8)

    def test_a_missing_unit_or_top_ratio_is_no_read(self) -> None:
        with self.assertRaises(BuildError) as cm:
            complete_from_words(TOPO, "x", {c: w for c, w in EXPECTED.items() if c != 20}, TOPS)
        self.assertEqual(cm.exception.code, "NO_READ")
        self.assertIn("EC3", cm.exception.message)
        with self.assertRaises(BuildError) as cm:
            complete_from_words(TOPO, "x", EXPECTED, {c: t for c, t in TOPS.items() if c != 4})
        self.assertEqual(cm.exception.code, "NO_READ")

    def test_a_word_that_is_not_whole_millivolts_is_refused(self) -> None:
        words = {c: list(w) for c, w in EXPECTED.items()}
        words[1][5] = VfWord(53, 0, False, -21).to_u32()  # -20.5 mV: no whole-mV value encodes to raw -21
        with self.assertRaises(BuildError) as cm:
            complete_from_words(TOPO, "x", words, TOPS)
        self.assertEqual(cm.exception.code, "NOT_REPRESENTABLE")


class Shape(unittest.TestCase):
    def base(self) -> Profile:
        return reference()

    def bad(self, **changes: object) -> str:
        with self.assertRaises(ProfileError) as cm:
            dataclasses.replace(self.base(), **changes)
        return str(cm.exception)

    def test_a_core_needs_its_top_ratio_and_offset(self) -> None:
        entries = list(self.base().entries)
        entries[0] = dataclasses.replace(entries[0], fused_top_ratio=None)
        self.assertIn("fused_top_ratio is missing", self.bad(entries=tuple(entries)))
        entries = list(self.base().entries)
        entries[0] = CoreSetting(entries[0].kind, entries[0].index, ratio=None, oc_voltage_mv=1200, offset_mv=None, fused_top_ratio=57)
        self.assertIn("offset_mv is missing", self.bad(entries=tuple(entries)))

    def test_every_fused_point_up_to_the_top_one(self) -> None:
        pts = [p for p in self.base().points if not ((p.kind, p.index) == ("E", 0) and p.point == 6)]
        self.assertIn("not at the top fused ratio", self.bad(points=tuple(pts)))
        pts = [p for p in self.base().points if not ((p.kind, p.index) == ("E", 0) and p.point == 3)]
        self.assertIn("are not 1..", self.bad(points=tuple(pts)))
        pts = [dataclasses.replace(p, expect_ratio=None) if (p.kind, p.index, p.point) == ("E", 0, 2) else p for p in self.base().points]
        self.assertIn("expect_ratio", self.bad(points=tuple(pts)))

    def test_a_point_without_a_core_and_one_turbo_table_are_refused(self) -> None:
        self.assertIn("no [[core]] entry", self.bad(points=(*self.base().points, PointSetting("E", 1, 1, 0, 8))))
        self.assertIn("both (e and p) or neither", self.bad(turbo=TurboTables(e=(48,) * 8)))

    def test_meta_complete_must_be_a_boolean(self) -> None:
        text = dump_profile(self.base()).replace("complete = true", 'complete = "yes"')
        with self.assertRaises(ProfileError):
            load_profile(text)

    def test_a_partial_profile_is_unchanged(self) -> None:
        prof = load_profile('[meta]\nname = "p"\nplatform = "arl-s"\n[[point]]\nkind = "P"\nindex = 0\npoint = 5\noffset_mv = -50\n')
        self.assertFalse(prof.complete)
        prof.check_complete(TOPO)  # a no-op for a partial profile

    def test_check_complete_names_a_missing_unit(self) -> None:
        base = self.base()
        entries = tuple(e for e in base.entries if (e.kind, e.index) != ("E", 12))
        points = tuple(p for p in base.points if (p.kind, p.index) != ("E", 12))
        with self.assertRaises(ProfileError) as cm:
            Profile("x", entries, base.turbo, points, complete=True).check_complete(TOPO)
        self.assertIn("EC3", str(cm.exception))


class Overlay(unittest.TestCase):
    def test_edits_replace_fields_and_points_only(self) -> None:
        base = reference()
        edits = Profile("e", (CoreSetting("E", 4, offset_mv=-10),), None, (PointSetting("P", 3, 5, -40, 53),))
        out = overlay(base, edits, TOPO, "mine")
        self.assertEqual((out.name, out.complete), ("mine", True))
        self.assertEqual({(e.kind, e.index): e.offset_mv for e in out.entries}[("E", 4)], -10)
        self.assertEqual({(p.kind, p.index, p.point): p.offset_mv for p in out.points}[("P", 3, 5)], -40)
        self.assertEqual(len(out.points), len(base.points))

    def test_an_edit_of_a_point_the_read_does_not_have_is_refused(self) -> None:
        with self.assertRaises(ProfileError):
            overlay(reference(), Profile("e", (), None, (PointSetting("E", 0, 9, -5),)), TOPO)


class Planner(unittest.TestCase):
    """``build_plan(only_changed=True)``: the words the CPU already holds are not written; the recorded top fused ratio is checked."""

    def baseline(self, prof: Profile, words: dict[int, list[int]] | None = None) -> tuple[dict[int, CoreReadings], dict[tuple[int, int], int]]:
        base: dict[int, CoreReadings] = {}
        pts: dict[tuple[int, int], int] = {}
        for c in TOPO.cores:
            vf = SIM.vf(c.cpu) if words is None else VfWord.from_u32(words[c.cpu][0])
            base[c.cpu] = CoreReadings(vf=vf, favored=SIM.favored_word(c.cpu), fused=SIM.fused_word(c.cpu), granular=SIM.granular_word(c.cpu))
            for n in range(1, 9):
                pts[(c.cpu, n)] = SIM.vf(c.cpu, 0, n).to_u32() if words is None else (words[c.cpu][n] if n < len(words[c.cpu]) else 0)
        return base, pts

    def plan(self, prof: Profile, words: dict[int, list[int]] | None = None, policy=None):
        base, pts = self.baseline(prof, words)
        return build_plan(prof, TOPO, base, per_core_enabled=True, policy=policy or _policy(), point_words=pts, only_changed=prof.complete)

    def test_a_cold_cpu_gets_only_the_points_that_differ(self) -> None:
        plan = self.plan(reference(None))
        writes = [s for s in plan.steps if s.kind.is_write]
        self.assertFalse([s for s in writes if s.kind is StepKind.WRITE_VF])  # 0x39 / 0 are already the accepted OC words
        self.assertTrue(writes and all(s.kind is StepKind.WRITE_VF_POINT for s in writes))
        self.assertTrue(plan.allowed, plan.refusals)

    def test_a_cpu_that_holds_the_profile_gets_no_write_and_no_0x15(self) -> None:
        full = {c.cpu: EXPECTED[TOPO.unit_cpus(TOPO.unit_of_cpu(c.cpu))[0]] for c in TOPO.cores}
        base, pts = self.baseline(reference(None), full)
        plan = build_plan(reference(None), TOPO, base, per_core_enabled=False, policy=_policy(), point_words=pts, only_changed=True)
        self.assertFalse([s for s in plan.steps if s.kind.is_write])

    def test_another_cpus_top_ratio_is_refused(self) -> None:
        prof = reference(None)
        entries = tuple(dataclasses.replace(e, fused_top_ratio=58) if (e.kind, e.index) == ("P", 0) else e for e in prof.entries)
        points = tuple(dataclasses.replace(p, expect_ratio=58) if (p.kind, p.index, p.point) == ("P", 0, 8) else p for p in prof.points)
        plan = self.plan(dataclasses.replace(prof, entries=entries, points=points))
        refused = [s for s in plan.steps if not s.verdict.allowed]
        self.assertTrue(any("fused_top_ratio" in s.verdict.detail for s in refused), [s.verdict.detail for s in refused])

    def test_there_are_no_rounds_without_a_step_limit(self) -> None:
        """D-049: no step limit in the policy, so a profile is always one round (``ramp_rounds`` is kept for its callers); the session-budget check of ``check_rounds`` stays."""
        prof = reference()
        base, pts = self.baseline(prof)
        self.assertEqual(ramp_rounds(prof, TOPO, base, pts, _policy(), only_changed=True), (1, None))
        self.assertEqual(ramp_rounds(prof, TOPO, base, pts, _policy(), only_changed=False), (1, None))
        self.assertEqual(check_rounds(1, None, 100, 10, _policy()), 110)
        with self.assertRaises(NeedsRounds):
            check_rounds(4, "P7 point 6", 300, 0, _policy(max_operations_per_session=1000))
        with self.assertRaises(NeedsRounds):
            check_rounds(MAX_RAMP_ROUNDS + 1, "P7 point 6", 1, 0, _policy())


def _policy(**changes: int):
    from arltuner.guard import load_policy

    pol = load_policy(REPO / "policies" / "default.toml", topology=TOPO)
    return dataclasses.replace(pol, **changes) if changes else pol
