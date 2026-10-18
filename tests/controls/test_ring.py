"""G-154 (D-045) controls tests: the ring (cache, P1 = 2) as one more unit of the planner -- reads, writes, the refusal of the ring unit alone, step-limit ramps, the transition-safe
order on the ring's own curve, the operation count and the ring walk. Pure (no hardware); the executor / guard / simulator flows are in tests/helper/test_ring_accepted.py and
tests/test_cli_ring.py.
"""
from __future__ import annotations

import unittest

from arltuner.controls import PlanError, StepKind, build_plan, count_operations, preflight, read_steps, resolve_ring_targets
from arltuner.controls import ring as ringmod
from arltuner.controls.order import reorder_writes
from arltuner.guard import AllowEntry
from arltuner.mailbox import Cmd, VfWord
from arltuner.profiles import Profile, RingPoint, RingSetting, load_profile
from arltuner.profiles.serialize import dump_profile
from arltuner.topology import arl_s_285k
from arltuner.vfdump import Pacer

from ._support import policy_with, profile

TOPO = arl_s_285k()
RATIOS = (8, 14, 20, 28, 34, 37, 38, 39, 40)


def word(ratio: int, offset_raw: int = 0) -> int:
    return VfWord(ratio, 0, False, offset_raw).to_u32()


def ring_policy(**changes: object):
    base = policy_with(**changes)
    entries = (
        AllowEntry("ring_point_read", Cmd.VOLTAGE_FREQ_READ, frozenset({2}), frozenset(range(1, 11)), "read"),
        AllowEntry("ring_point_write", Cmd.VOLTAGE_FREQ_WRITE, frozenset({2}), frozenset(range(1, 10)), "write", "vf_point_offset_bounded"),
    )
    import dataclasses

    return dataclasses.replace(base, allow=(*base.allow, *entries))


def ring_profile(offsets: dict[int, int], oc: int = 0x28, name: str = "r") -> Profile:
    return Profile(name, (), None, (), False, RingSetting(oc, tuple(RingPoint(n, offsets.get(n, 0), r) for n, r in enumerate(RATIOS, start=1))))


def live(offsets: dict[int, int] | None = None, oc: int = 0x28, points: int = 9) -> dict[int, int]:
    """The ring channel as a read gives it: key 0 = the OC word, k = point k (raw offsets)."""
    return {0: oc, **{n: word(r, (offsets or {}).get(n, 0)) for n, r in enumerate(RATIOS[:points], start=1)}}


def plan(profile: Profile, words: dict[int, int], policy=None, **kw):
    return build_plan(profile, TOPO, {}, per_core_enabled=True, policy=policy or ring_policy(), ring_words=words, **kw)


class Targets(unittest.TestCase):
    def test_the_ring_targets_are_addressed_through_cpu_0(self) -> None:
        targets = resolve_ring_targets(ring_profile({8: -25, 9: -25}), TOPO)
        self.assertEqual([(t.cpu, t.point, t.offset_mv, t.expect_ratio, t.oc_word) for t in targets][-2:], [(0, 8, -25, 39, 0x28), (0, 9, -25, 40, 0x28)])
        self.assertEqual(resolve_ring_targets(profile('kind = "P"\nindex = 4\noffset_mv = -30'), TOPO), ())

    def test_a_ring_only_profile_has_no_0x14_read_and_the_ring_reads_are_p1_2(self) -> None:
        steps = read_steps((), ring_policy(), ring_profile({}), TOPO)
        self.assertEqual([(s.kind, s.cmd, s.p1, s.p2) for s in steps], [(StepKind.READ_RING_OC, 0x10, 2, 0), *((StepKind.READ_RING_POINT, 0x10, 2, n) for n in range(1, 10))])
        self.assertTrue(all(s.verdict.allowed and s.cpu == 0 for s in steps))
        self.assertTrue(all(s.kind.is_ring for s in steps))

    def test_the_ring_reads_follow_the_core_reads_in_a_mixed_profile(self) -> None:
        base = profile('kind = "P"\nindex = 4\noffset_mv = -30')
        import dataclasses

        mixed = dataclasses.replace(base, ring=ring_profile({}).ring)
        kinds = [s.kind for s in read_steps(resolve_targets_of(mixed), ring_policy(), mixed, TOPO)]
        self.assertEqual(kinds[0], StepKind.READ_PERCORE_ENABLE)
        self.assertEqual(kinds[-10:], [StepKind.READ_RING_OC, *[StepKind.READ_RING_POINT] * 9])

    def test_the_operation_count_includes_the_ring(self) -> None:
        self.assertEqual(count_operations((), None, ring_points=9), 10 + 9 + 9)  # the OC word + 9 point reads, 9 writes, 9 read-backs; no 0x14 / 0x15
        from arltuner.controls import resolve_targets

        t = resolve_targets(profile('kind = "P"\nindex = 4\noffset_mv = -30'), TOPO)
        self.assertEqual(count_operations(t, None, ring_points=9) - count_operations(t, None), 10 + 9 + 9)

    def test_preflight_counts_the_ring_and_does_not_stop_the_cores_for_a_ring_the_policy_refuses(self) -> None:
        import dataclasses

        from arltuner.controls import resolve_targets  # noqa: F401

        base = profile('kind = "P"\nindex = 4\noffset_mv = -30')
        mixed = dataclasses.replace(base, ring=ring_profile({8: -25}).ring)
        no_ring = policy_with()  # no ring entry at all (default deny)
        targets = preflight(mixed, TOPO, no_ring)  # the ring is judged by the plan, not by pre-flight: no PlanRefused
        self.assertEqual(len(targets), 1)


def resolve_targets_of(p: Profile):
    from arltuner.controls import resolve_targets

    return resolve_targets(p, TOPO)


class Plan(unittest.TestCase):
    def test_only_the_ring_points_that_differ_are_written_and_never_the_oc_word(self) -> None:
        p = plan(ring_profile({8: -25, 9: -25}), live(), only_changed=True)  # -25 mV = raw -26: the words 0xFCC00027 / 0xFCC00028 of the reference machine
        writes = [s for s in p.steps if s.kind.is_write]
        self.assertEqual({(s.kind, s.cmd, s.p1) for s in writes}, {(StepKind.WRITE_RING_POINT, 0x11, 2)})
        self.assertEqual(sorted((s.p2, s.data) for s in writes), [(8, 0xFCC00027), (9, 0xFCC00028)])
        self.assertNotIn(0, [s.p2 for s in writes])
        self.assertEqual(p.ring_refused, ())
        self.assertTrue(all(s.verdict.allowed for s in p.steps))
        self.assertTrue(p.allowed)

    def test_a_point_that_already_holds_the_offset_is_not_written(self) -> None:
        p = plan(ring_profile({8: -25, 9: -25}), live({8: -26, 9: -26}), only_changed=True)
        self.assertEqual([s for s in p.steps if s.kind.is_write], [])

    def test_the_ring_oc_word_differing_refuses_the_ring_unit_alone_by_name(self) -> None:
        p = plan(ring_profile({8: -25, 9: -25}), live(oc=0x27, points=8), only_changed=True)
        self.assertEqual([s for s in p.steps if s.kind.is_write], [])
        self.assertTrue(any(r.startswith("ring_oc_differs (planner)") and "0x00000027" in r and "0x00000028" in r for r in p.ring_refused), p.ring_refused)
        self.assertTrue(any("ring point 9 does not exist" in r for r in p.ring_refused), p.ring_refused)
        self.assertTrue(any(w.startswith("ring not written:") for w in p.warnings))
        self.assertTrue(p.allowed, "the refusal of the ring does not make the rest of the plan refused")

    def test_a_missing_ring_oc_word_refuses_the_ring(self) -> None:
        words = live()
        del words[0]
        p = plan(ring_profile({8: -26}), words, only_changed=True)
        self.assertTrue(any("ring_oc_unread" in r for r in p.ring_refused), p.ring_refused)

    def test_the_expect_ratio_guards_each_point(self) -> None:
        words = live()
        words[7] = word(36)  # reads 36x, the profile expects 38x
        p = plan(ring_profile({8: -26}), words, only_changed=True)
        self.assertEqual([s for s in p.steps if s.kind.is_write], [])
        self.assertTrue(any("expect_ratio (planner)" in r and "ring point 7" in r for r in p.ring_refused), p.ring_refused)

    def test_a_policy_without_the_ring_write_entry_refuses_the_ring_with_the_guards_reason(self) -> None:
        import dataclasses

        base = ring_policy()
        policy = dataclasses.replace(base, allow=tuple(e for e in base.allow if e.name != "ring_point_write"))
        p = plan(ring_profile({8: -25}), live(), policy=policy, only_changed=True)
        self.assertEqual([s for s in p.steps if s.kind.is_write], [])
        self.assertTrue(any("ring point 8: NOT_ALLOWLISTED" in r for r in p.ring_refused), p.ring_refused)

    def test_a_policy_without_the_ring_read_entry_names_the_unreadable_word(self) -> None:
        import dataclasses

        policy = dataclasses.replace(policy_with())
        p = plan(ring_profile({8: -26}), {}, policy=policy, only_changed=True)  # nothing could be read
        self.assertTrue(any("was not read: the guard refuses the read (NOT_ALLOWLISTED" in r for r in p.ring_refused), p.ring_refused)

    def test_a_big_ring_offset_is_one_write_with_a_large_change_warning_and_no_next_profile(self) -> None:
        p = plan(ring_profile({8: -45}), live(), policy=ring_policy(), only_changed=True)  # D-049: no step limit
        self.assertTrue(p.allowed, p.refusals)
        self.assertFalse(p.needs_ramp)
        self.assertIsNone(p.next_profile_text)
        self.assertEqual([s.p2 for s in p.steps if s.kind.is_write], [8])
        self.assertEqual(p.ring_refused, ())
        self.assertFalse(any(w.startswith("LARGE_CHANGE") for w in p.warnings))  # 45 mV is below the 50 mV threshold
        big = plan(ring_profile({8: -120}), live(), policy=ring_policy(), only_changed=True)
        self.assertTrue(big.allowed, big.refusals)
        self.assertTrue(any(w.startswith("LARGE_CHANGE") and "ring point 8" in w for w in big.warnings), big.warnings)

    def test_the_writes_are_ordered_raising_first_on_the_rings_own_curve(self) -> None:
        p = plan(ring_profile({7: -20, 8: 10}), live(), policy=ring_policy(), only_changed=True)
        order = [s.p2 for s in p.steps if s.kind.is_write]
        self.assertEqual(order, [8, 7])  # the raise of point 8 goes before the lowering of point 7

    def test_ring_steps_never_collide_with_a_core_of_cpu_0(self) -> None:
        """The ring is read through CPU 0 but is not P-core 0: its words are their own channel, the P-core 0 point 8 is judged separately."""
        import dataclasses

        base = load_profile(dump_profile(point_profile_p0_point8(-10)))
        mixed = dataclasses.replace(base, ring=ring_profile({8: -26}).ring)
        p = build_plan(mixed, TOPO, {}, per_core_enabled=True, policy=ring_policy(), point_words={(0, 8): word(57)}, ring_words=live(), only_changed=True)
        kinds = sorted((s.kind.value, s.p1, s.p2) for s in p.steps if s.kind.is_write)
        self.assertEqual(kinds, [("write_ring_point", 2, 8), ("write_vf_point", 0, 8)])

    def test_a_ring_only_plan_reads_no_0x14_and_writes_no_0x15(self) -> None:
        p = build_plan(ring_profile({8: -26}), TOPO, {}, per_core_enabled=None, policy=ring_policy(), ring_words=live(), only_changed=True)
        self.assertFalse([s for s in p.steps if s.kind in (StepKind.WRITE_PERCORE_ENABLE, StepKind.READ_PERCORE_ENABLE)])

    def test_a_profile_without_a_ring_never_plans_a_ring_step(self) -> None:
        p = build_plan(profile('kind = "P"\nindex = 4\noffset_mv = -30'), TOPO, {4: __import__("tests.controls._support", fromlist=["P4_READINGS"]).P4_READINGS}, per_core_enabled=True, policy=policy_with())
        self.assertFalse([s for s in p.steps if s.kind.is_ring])
        self.assertEqual((p.ring_targets, p.ring_refused), ((), ()))


def point_profile_p0_point8(offset_mv: int) -> Profile:
    from arltuner.profiles import PointSetting

    return Profile("p", (), None, (PointSetting("P", 0, 8, offset_mv, 57),))


class Order(unittest.TestCase):
    def test_the_ring_is_its_own_unit_and_is_modelled_from_the_ring_words(self) -> None:
        p = plan(ring_profile({7: -20, 8: 10}), live(), policy=ring_policy(), only_changed=True)
        steps = [s for s in p.steps if s.kind.is_write]
        ordered, notes = reorder_writes(list(reversed(steps)), TOPO, {}, {}, live())
        self.assertEqual(notes, [])
        self.assertEqual([s.p2 for s in ordered], [8, 7])


class Walk(unittest.TestCase):
    class Resp:
        def __init__(self, rc: int, data: int) -> None:
            self.response = type("R", (), {"rc": rc, "data": data})()

    def run_walk(self, table: dict[int, tuple[int, int]], cap: int = 10):
        seen: list[int] = []

        def execute(req):
            seen.append(req.p2)
            assert req.p1 == 2 and req.cmd == 0x10 and req.cpu == 0
            return self.Resp(*table.get(req.p2, (0x11, 0)))

        pacer = Pacer(0.0, lambda: 0.0, lambda s: None)
        curve, stop = ringmod.walk(0, execute, pacer, str, cap)
        return curve, stop, seen

    def test_the_walk_ends_at_the_first_rc_0x11(self) -> None:
        table = {0: (0, 0x28), **{n: (0, word(r)) for n, r in enumerate(RATIOS, start=1)}}
        curve, stop, seen = self.run_walk(table)
        self.assertIsNone(stop)
        self.assertEqual((curve.oc, len(curve.points), curve.end), (0x28, 9, "rc 0x11 at P2=10"))
        self.assertEqual(seen, list(range(11)))

    def test_a_word_without_a_ratio_ends_the_table_like_a_simulator_that_answers_every_p2(self) -> None:
        table = {0: (0, 0x28), 1: (0, word(8)), 2: (0, 0)}
        curve, stop, _ = self.run_walk({**table, **{n: (0, 0) for n in range(3, 11)}})
        self.assertEqual((len(curve.points), curve.end), (1, "empty word at P2=2"))

    def test_the_cap_ends_the_walk_and_says_so(self) -> None:
        table = {n: (0, word(8 + n)) for n in range(0, 12)}
        curve, stop, seen = self.run_walk(table, cap=3)
        self.assertEqual((seen, curve.end), ([0, 1, 2, 3], "cap reached (P2=3), no rc 0x11 seen"))

    def test_another_rc_stops_the_walk(self) -> None:
        curve, stop, _ = self.run_walk({0: (0, 0x28), 1: (0x05, 0)})
        self.assertIsNotNone(stop)
        self.assertEqual(stop.code, 3)
        self.assertEqual(curve.oc, 0x28)

    def test_walk_cap_follows_the_policy_entries(self) -> None:
        self.assertEqual(ringmod.walk_cap(ring_policy()), 10)
        self.assertIsNone(ringmod.walk_cap(policy_with()))
        self.assertEqual(ringmod.ring_cpu(TOPO), 0)

    def test_the_json_round_trips_to_words(self) -> None:
        table = {0: (0, 0x28), **{n: (0, word(r, -26 if n >= 8 else 0)) for n, r in enumerate(RATIOS, start=1)}}
        curve, _, _ = self.run_walk(table)
        doc = ringmod.to_json(curve)
        self.assertEqual(ringmod.from_json(doc), curve.words)
        self.assertIsNone(ringmod.from_json(None))
        self.assertIsNone(ringmod.from_json({"oc": None}))
        self.assertIn("table end: rc 0x11 at P2=10", "\n".join(ringmod.format_lines(curve)))


if __name__ == "__main__":
    unittest.main()
