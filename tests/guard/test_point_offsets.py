"""T-025 guard tests: per-V/F-point offsets -- ``0x11`` with ``P1=0`` and ``P2`` 1..15 under the check ``vf_point_offset_bounded``, distinct from the
whole-curve offset (``P2 = 0``, ``vf_bounded``).

Measured (``[MEASURED lnx 2026-10-02]``, docs/knowledge/registers.md, E CPU 8): ``0x10 P2=6`` = ``0x0000002E`` (point 6 = 46x, V target 0, offset
0). OCCT wrote point words as ``ratio | offset << 21`` with V target 0 (docs/knowledge/registers.md). Whether the hardware accepts a point write on
Linux is ``[UNVERIFIED]``: these tests prove only what the *guard* lets through or refuses, against a recording transport.

Same rules as test_guard.py: every refusal sends nothing, and each test is written so that removing the rule it names makes it fail.
"""
from __future__ import annotations

import dataclasses
import unittest
from pathlib import Path
from types import SimpleNamespace

from arltuner.guard import AllowEntry, GuardRefused, PolicyError, Refusal, default_m1_policy, load_policy
from arltuner.guard.policy import CHECKS, CheckContext, build_context
from arltuner.mailbox import Cmd, VfWord

from .test_guard import GuardCase

POINT6 = 0x0000002E  # [MEASURED lnx 2026-10-02] E CPU 8 point 6: ratio 46, V target 0, offset 0
E8 = 8
P1CPU = 1


def point_word(ratio: int = 46, offset_raw: int = 0, v_raw: int = 0, static: bool = False) -> int:
    return VfWord(ratio, v_raw, static, offset_raw).to_u32()


def with_points(policy, **changes):
    """``policy`` plus the two point entries (reads of ``0x10 P2 1..15``, writes of ``0x11 P2 1..15`` under the new check)."""
    entries = (
        AllowEntry("vf_point_read", Cmd.VOLTAGE_FREQ_READ, frozenset({0}), frozenset(range(1, 16)), "read"),
        AllowEntry("vf_point_write", Cmd.VOLTAGE_FREQ_WRITE, frozenset({0}), frozenset(range(1, 16)), "write", "vf_point_offset_bounded"),
    )
    return dataclasses.replace(policy, allow=(*policy.allow, *entries), **changes)


class PointCase(GuardCase):
    def setUp(self) -> None:
        super().setUp()
        self.policy = with_points(dataclasses.replace(self.policy, max_operations_per_session=40, max_operations_per_boot=400))

    def open(self, cpu: int = E8, point: int = 6, word: int = POINT6, **changes):
        if changes:
            self.policy = dataclasses.replace(self.policy, **changes)
        self.transport.state[(cpu, 0x10, 0, point)] = word
        guard = self.make_guard()
        guard.execute(self.read_req(cpu, 0x10, p2=point), self.confirm)
        return guard

    def write(self, guard, data: int, cpu: int = E8, point: int = 6):
        return guard.execute(self.write_req(data, cpu=cpu, p2=point), self.confirm)

    def refuse(self, guard, data: int, fragment: str, cpu: int = E8, point: int = 6) -> GuardRefused:
        exc = self.assert_refused(guard, self.write_req(data, cpu=cpu, p2=point), Refusal.CHECK_FAILED)
        self.assertIn(fragment, exc.detail)
        self.assertFalse(guard.tripped)
        return exc


class InertByDefault(GuardCase):
    def test_the_shipped_policy_allows_neither_point_reads_nor_point_writes(self) -> None:
        self.policy = dataclasses.replace(default_m1_policy(), max_operations_per_session=10, min_dwell_s=0.0)
        guard = self.make_guard()
        self.transport.state[(E8, 0x10, 0, 6)] = POINT6
        for p2 in (1, 6, 8, 15):
            self.assert_refused(guard, self.read_req(E8, 0x10, p2=p2), Refusal.NOT_ALLOWLISTED)
            self.assert_refused(guard, self.write_req(point_word(46, -5), cpu=E8, p2=p2), Refusal.NOT_ALLOWLISTED)
        self.assertEqual(self.transport.calls, [])

    def test_the_default_policy_has_no_entry_with_the_point_check(self) -> None:
        self.assertNotIn("vf_point_offset_bounded", [e.check for e in default_m1_policy().allow])
        for entry in default_m1_policy().allow:
            if entry.cmd in (0x10, 0x11):
                self.assertEqual(entry.p2, frozenset({0}), entry.name)  # every shipped 0x10 / 0x11 entry is P2 = 0 only


class AcceptedPointOffset(PointCase):
    def test_an_offset_only_word_is_sent_read_back_and_verified(self) -> None:
        guard = self.open()
        data = point_word(46, -10)
        result = self.write(guard, data)
        self.assertIs(result.verified, True)
        self.assertEqual(self.transport.calls[-2:], [(E8, 0x11, 0, 6, data), (E8, 0x10, 0, 6, 0)])  # the read-back is the same point's 0x10
        self.assertEqual(self.transport.state[(E8, 0x10, 0, 6)], data)
        self.assertEqual(data & 0xFFFFF, POINT6)  # ratio and V target untouched, only [31:21] changed

    def test_each_point_of_an_e_cpu_and_a_p_cpu_is_judged_alone(self) -> None:
        for cpu, point, ratio in ((E8, 1, 30), (E8, 6, 46), (P1CPU, 8, 57)):
            with self.subTest(cpu=cpu, point=point):
                self.transport.state.clear()
                guard = self.open(cpu, point, point_word(ratio))
                self.assertIs(self.write(guard, point_word(ratio, -5), cpu, point).verified, True)

    def test_there_is_no_window_floor_or_ceiling(self) -> None:
        # D-049: the D-015 floors (E -102 raw, P -51 raw with the assumed BIOS offset) and the offset ceiling are gone: the 11-bit field is the only bound
        guard = self.open(E8, 6, POINT6)
        for raw in (-102, -103, -500, -1024):
            self.assertIs(self.write(guard, point_word(46, raw)).verified, True, raw)
        self.transport.state.clear()
        # a positive offset is bounded only by the CPU's 0x28 limit (bound here as 0x7FF) from the point's own V target: 0x400 + 1023 == the limit
        guard = self.open(E8, 6, point_word(46, 0, v_raw=0x400), hw_v_target_max_raw={"P": 0x7FF, "E": 0x7FF})
        for raw in (26, 300, 1023):
            self.assertIs(self.write(guard, point_word(46, raw, v_raw=0x400)).verified, True, raw)
        self.transport.state.clear()
        guard = self.open(P1CPU, 8, point_word(57))
        for raw in (-51, -52, -400):
            self.assertIs(self.write(guard, point_word(57, raw), P1CPU, 8).verified, True, raw)

    def test_entry_params_can_only_tighten_the_window(self) -> None:
        entry = AllowEntry(
            "vf_point_write", Cmd.VOLTAGE_FREQ_WRITE, frozenset({0}), frozenset(range(1, 16)), "write", "vf_point_offset_bounded",
            {"offset_raw_min": -20},
        )
        self.policy = dataclasses.replace(self.policy, allow=(*[e for e in self.policy.allow if e.name != "vf_point_write"], entry))
        guard = self.open()
        self.refuse(guard, point_word(46, -21), "outside")
        self.assertIs(self.write(guard, point_word(46, -20)).verified, True)


class OnlyTheOffsetMayChange(PointCase):
    def test_ratio_must_equal_the_baseline_ratio(self) -> None:
        guard = self.open()
        for ratio in (45, 47, 0, 255):
            self.refuse(guard, point_word(ratio, -5), "ratio")

    def test_v_target_must_equal_the_baseline_v_target(self) -> None:
        guard = self.open()
        self.refuse(guard, point_word(46, -5, v_raw=1), "V target")  # a voltage on a fused point is not an offset write
        self.refuse(guard, point_word(46, -5, v_raw=0x4A0), "V target")

    def test_mode_bit_must_equal_the_baseline_mode(self) -> None:
        guard = self.open()
        self.refuse(guard, point_word(46, -5, static=True), "mode bit")

    def test_a_baseline_with_a_v_target_keeps_it_exactly(self) -> None:
        guard = self.open(word=point_word(46, 0, v_raw=0x123))
        self.refuse(guard, point_word(46, -5, v_raw=0), "V target")
        self.assertIs(self.write(guard, point_word(46, -5, v_raw=0x123)).verified, True)

    def test_a_zero_ratio_or_static_baseline_is_refused(self) -> None:
        guard = self.open(word=0)
        self.refuse(guard, point_word(0, -5), "ratio 0")
        self.transport.state.clear()
        guard = self.open(word=point_word(46, 0, static=True))
        self.refuse(guard, point_word(46, -5, static=True), "static")

    def test_there_is_no_step_limit(self) -> None:
        guard = self.open()
        self.assertIs(self.write(guard, point_word(46, -200)).verified, True)  # D-049: 200 raw in one write

    def test_a_baseline_with_an_offset_moves_by_any_amount(self) -> None:
        guard = self.open(word=point_word(46, -50, v_raw=0x400), hw_v_target_max_raw={"P": 0x7FF, "E": 0x7FF})
        self.assertIs(self.write(guard, point_word(46, -300, v_raw=0x400)).verified, True)
        self.assertIs(self.write(guard, point_word(46, 100, v_raw=0x400)).verified, True)


class BaselineIsThePointsOwn(PointCase):
    def test_no_write_without_a_read_of_the_same_cpu_and_point(self) -> None:
        self.transport.state[(E8, 0x10, 0, 5)] = point_word(40)
        guard = self.make_guard()
        guard.execute(self.read_req(E8, 0x10, p2=5), self.confirm)
        self.assert_refused(guard, self.write_req(point_word(46, -5), cpu=E8, p2=6), Refusal.BASELINE_MISSING)  # point 5's read is not point 6's

    def test_the_oc_point_baseline_does_not_stand_in_for_a_point(self) -> None:
        self.transport.state[(E8, 0x10, 0, 0)] = point_word(48, 0, v_raw=0x4A0)
        guard = self.make_guard()
        guard.execute(self.read_req(E8, 0x10), self.confirm)
        self.assert_refused(guard, self.write_req(point_word(46, -5), cpu=E8, p2=6), Refusal.BASELINE_MISSING)

    def test_another_cpus_point_baseline_does_not_count(self) -> None:
        self.transport.state[(9, 0x10, 0, 6)] = POINT6
        guard = self.make_guard()
        guard.execute(self.read_req(9, 0x10, p2=6), self.confirm)
        self.assert_refused(guard, self.write_req(point_word(46, -5), cpu=E8, p2=6), Refusal.BASELINE_MISSING)

    def test_a_failed_read_is_no_baseline(self) -> None:
        self.transport.state[(E8, 0x10, 0, 6)] = POINT6
        self.transport.read_rc = 0x11  # the hardware's answer for a point that does not exist
        guard = self.make_guard()
        guard.execute(self.read_req(E8, 0x10, p2=6), self.confirm)
        self.assert_refused(guard, self.write_req(point_word(46, -5), cpu=E8, p2=6), Refusal.BASELINE_MISSING)


class ReadBackAndRevert(PointCase):
    def test_a_read_back_that_differs_trips_the_guard(self) -> None:
        guard = self.open()
        self.transport.apply_write = lambda data: data ^ (1 << 21)  # the point took another offset
        result = self.write(guard, point_word(46, -10))
        self.assertIs(result.verified, False)
        self.assertTrue(guard.tripped)
        self.assert_refused(guard, self.write_req(point_word(46, -5), cpu=E8, p2=6), Refusal.TRIPPED)

    def test_revert_is_the_baseline_point_word_exactly_never_filled_from_0x09(self) -> None:
        self.transport.state[(E8, 0x09, 0x11, 0)] = 0x4A02E  # the fused OC-point voltage 0x4A0: must NOT leak into a point word
        guard = self.open()
        guard.execute(self.read_req(E8, 0x09, p1=0x11), self.confirm)
        self.write(guard, point_word(46, -10))
        undo = guard.revert_plan()
        self.assertEqual([(r.cpu, r.cmd, r.p1, r.p2, r.data, r.purpose) for r in undo], [(E8, 0x11, 0, 6, POINT6, "revert")])

    def test_two_writes_undo_newest_first_through_the_state_each_left(self) -> None:
        guard = self.open()
        first, second = point_word(46, -10), point_word(46, -20)
        self.write(guard, first)
        self.write(guard, second)
        self.assertEqual([r.data for r in guard.revert_plan()], [first, POINT6])

    def test_the_revert_word_passes_the_same_check(self) -> None:
        guard = self.open()
        self.write(guard, point_word(46, -10))
        (undo,) = guard.revert_plan()
        self.assertIs(guard.execute(undo, self.confirm).verified, True)
        self.assertEqual(self.transport.state[(E8, 0x10, 0, 6)], POINT6)

    def test_a_point_write_feeds_no_0x1d_and_no_oc_point_fact(self) -> None:
        guard = self.open()
        self.transport.state[(E8, 0x1C, 0, 0)] = 0x2E30
        guard.execute(self.read_req(E8, 0x1C), self.confirm)
        self.write(guard, point_word(46, -10))
        exc = self.assert_refused(guard, self.write_req(46, cpu=E8, cmd=0x1D), Refusal.CHECK_FAILED)  # no OC-point 0x11 was executed
        self.assertIn("no 0x11 word", exc.detail)

    def test_the_0x1d_revert_follows_the_oc_point_word_not_a_point_word(self) -> None:
        self.transport.state[(E8, 0x10, 0, 0)] = point_word(48, 0, v_raw=0x4A0)
        self.transport.state[(E8, 0x1C, 0, 0)] = 0x2E30
        self.transport.state[(E8, 0x09, 0x11, 0)] = 0x4A02E
        guard = self.open()
        for cmd in (0x10, 0x1C, 0x09):
            guard.execute(self.read_req(E8, cmd, p1=0x11 if cmd == 0x09 else 0), self.confirm)
        self.write(guard, point_word(46, -10))  # point 6 first ...
        guard.execute(self.write_req(point_word(48, -5, v_raw=0x4A0), cpu=E8), self.confirm)  # ... then the OC point
        self.transport.state[(E8, 0x1C, 0, 0)] = 0x2E30
        guard.execute(self.write_req(48, cpu=E8, cmd=0x1D), self.confirm)
        undo = {(r.cmd, r.p2): r.data for r in guard.revert_plan()}
        self.assertEqual(undo[(0x11, 6)], POINT6)
        self.assertEqual(VfWord.from_u32(undo[(0x11, 0)]).ratio, 48)
        self.assertEqual(undo[(0x1D, 0)], 48)  # the OC point's undo ratio, not the point's 46


class CheckLevel(unittest.TestCase):
    """The predicates themselves: fail closed on a missing context, a wrong address, and the OC point."""

    def ctx(self, **kw) -> CheckContext:
        return CheckContext(**{"bound": True, **kw})

    def req(self, data: int, p1: int = 0, p2: int = 6):
        return SimpleNamespace(cpu=E8, cmd=0x11, p1=p1, p2=p2, data=data, purpose="t")

    def test_registry_has_both_vf_checks(self) -> None:
        self.assertIn("vf_point_offset_bounded", CHECKS)
        self.assertIn("vf_bounded", CHECKS)

    def test_no_context_is_a_refusal(self) -> None:
        self.assertFalse(CHECKS["vf_point_offset_bounded"](self.req(point_word(46, -5)), POINT6, {}, None)[0])
        self.assertFalse(CHECKS["vf_point_offset_bounded"](self.req(point_word(46, -5)), POINT6, {}, CheckContext())[0])

    def test_the_oc_point_p2_0_is_refused_by_the_point_check(self) -> None:
        ok, detail = CHECKS["vf_point_offset_bounded"](self.req(point_word(46, -5), p2=0), POINT6, {}, self.ctx())
        self.assertFalse(ok)
        self.assertIn("OC point", detail)

    def test_p2_16_and_p1_not_0_or_2_are_refused(self) -> None:
        for p1, p2 in ((0, 16), (1, 6), (7, 1), (3, 6), (0, 255), (2, 16)):
            self.assertFalse(CHECKS["vf_point_offset_bounded"](self.req(point_word(46, -5), p1, p2), POINT6, {}, self.ctx())[0], (p1, p2))

    def test_vf_bounded_refuses_a_point_address(self) -> None:
        # a policy that (mis)routed P2 != 0 through vf_bounded would judge a point word by OC-point rules: refuse that at the check as well
        ctx = build_context(default_m1_policy(), E8, favored_word=0x2E30, fused_word=0x4A02E, vf_word_sent=None)
        base = point_word(48, 0, v_raw=0x4A0)
        ok, _ = CHECKS["vf_bounded"](self.req(point_word(48, -5, v_raw=0x4A0), p2=0), base, {}, ctx)
        self.assertTrue(ok)
        ok, detail = CHECKS["vf_bounded"](self.req(point_word(48, -5, v_raw=0x4A0), p2=6), base, {}, ctx)
        self.assertFalse(ok)
        self.assertIn("P2 6", detail)


class PolicyBinding(unittest.TestCase):
    """A policy cannot bind the point check to the OC point or the whole-curve check to a point."""

    def entry(self, check: str, p1=(0,), p2=(1,), cmd=0x11, name="e"):
        return AllowEntry(name, cmd, frozenset(p1), frozenset(p2), "write", check)

    def test_point_check_cannot_cover_p2_0(self) -> None:
        with self.assertRaises(PolicyError):
            self.entry("vf_point_offset_bounded", p2=(0, 1))

    def test_point_check_cannot_cover_p2_above_15_or_other_domains(self) -> None:
        for kwargs in (dict(p2=(16,)), dict(p1=(1,)), dict(p1=(7,)), dict(p1=(0, 7)), dict(p1=(2, 7))):
            with self.subTest(kwargs=kwargs), self.assertRaises(PolicyError):
                self.entry("vf_point_offset_bounded", **kwargs)

    def test_point_check_needs_cmd_0x11(self) -> None:
        with self.assertRaises(PolicyError):
            self.entry("vf_point_offset_bounded", cmd=0x15)

    def test_whole_curve_check_cannot_cover_a_point(self) -> None:
        with self.assertRaises(PolicyError):
            self.entry("vf_bounded", p2=(0, 6))
        self.entry("vf_bounded", p2=(0,))  # the shipped shape

    def test_point_entries_may_not_overlap_each_other(self) -> None:
        a = self.entry("vf_point_offset_bounded", p2=(1, 2), name="a")
        b = self.entry("vf_point_offset_bounded", p2=(2, 3), name="b")
        with self.assertRaises(PolicyError):
            dataclasses.replace(default_m1_policy(), allow=(a, b))

    TOML = """
cpus = ["0-23"]
max_operations_per_session = 20
max_operations_per_boot = 50

[[allow]]
name = "vf_point_read"
cmd = 0x10
p1 = [0]
p2 = [1, 2, 3, 4, 5, 6, 7, 8]
kind = "read"

[[allow]]
name = "vf_point_write"
cmd = 0x11
p1 = [0]
p2 = [1, 2, 3, 4, 5, 6, 7, 8]
kind = "write"
check = "vf_point_offset_bounded"
params = { offset_raw_min = -40 }
"""

    def test_a_toml_policy_loads_the_point_entries(self) -> None:
        policy = load_policy(self.TOML)
        write = policy.match(0x11, 0, 6)
        assert write is not None
        self.assertEqual((write.check, dict(write.params)), ("vf_point_offset_bounded", {"offset_raw_min": -40}))
        self.assertIsNone(policy.match(0x11, 0, 0))  # the OC point is not covered
        self.assertIsNone(policy.match(0x11, 0, 9))

    def test_a_toml_policy_that_binds_the_point_check_to_p2_0_does_not_load(self) -> None:
        with self.assertRaises(PolicyError):
            load_policy(self.TOML.replace("p2 = [1, 2, 3, 4, 5, 6, 7, 8]\nkind = \"write\"", "p2 = [0, 1]\nkind = \"write\""))


class ExamplePolicy(unittest.TestCase):
    """``examples/reference-285k/policies/example-oc-ramp.toml``: the point entries are comments (inert); uncommented they load and bind the check correctly."""

    PATH = Path(__file__).resolve().parents[2] / "examples" / "reference-285k" / "policies" / "example-oc-ramp.toml"

    def test_the_example_is_inert_for_points_as_shipped(self) -> None:
        policy = load_policy(self.PATH)
        for p2 in range(1, 16):
            self.assertIsNone(policy.match(0x10, 0, p2))
            self.assertIsNone(policy.match(0x11, 0, p2))

    def test_the_commented_entries_load_when_uncommented(self) -> None:
        text = self.PATH.read_text(encoding="utf-8")
        marker = "# Uncomment both entries"
        head, _, tail = text.partition(marker)
        block = [ln[2:] if ln.startswith("# ") else ln[1:] for ln in tail.splitlines()[1:] if ln.startswith("#") and not ln.startswith("# #")]
        policy = load_policy(head + "\n" + "\n".join(block) + "\n")
        self.assertEqual({p2 for p2 in range(0, 16) if policy.match(0x10, 0, p2)}, set(range(0, 10)))  # 0 is the shipped OC-point read
        write = policy.match(0x11, 0, 8)
        assert write is not None
        self.assertEqual(write.check, "vf_point_offset_bounded")
        self.assertIsNone(policy.match(0x11, 0, 9))
        oc = policy.match(0x11, 0, 0)
        assert oc is not None
        self.assertEqual(oc.check, "vf_bounded")


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------------------------------------------------
# T-028: ring points (P1 = 2) through the same check
# ---------------------------------------------------------------------------------------------------------------------
RING8 = 0xF6600027  # [MEASURED lnx 2026-10-02] ring point 8: 39x, offset -75.2 mV (raw -77), set by the BIOS


def with_ring(policy, p1=(0, 2), **changes):
    """``policy`` plus entries for ``0x10`` / ``0x11`` P2 1..15 on the domains in ``p1`` (the entry binding is what lets P1 2 through)."""
    entries = (
        AllowEntry("pt_read", Cmd.VOLTAGE_FREQ_READ, frozenset(p1), frozenset(range(1, 16)), "read"),
        AllowEntry("pt_write", Cmd.VOLTAGE_FREQ_WRITE, frozenset(p1), frozenset(range(1, 16)), "write", "vf_point_offset_bounded"),
    )
    return dataclasses.replace(policy, allow=(*policy.allow, *entries), **changes)


class RingPoints(GuardCase):
    def setUp(self) -> None:
        super().setUp()
        self.base_policy = dataclasses.replace(self.policy, max_operations_per_session=40, max_operations_per_boot=400)
        self.policy = with_ring(self.base_policy)
        self.transport.state[(0, 0x10, 2, 8)] = RING8

    def open(self):
        guard = self.make_guard()
        guard.execute(self.read_req(0, 0x10, p1=2, p2=8), self.confirm)
        return guard

    def test_a_ring_point_offset_write_is_allowed_and_read_back(self) -> None:
        guard = self.open()
        new = point_word(39, -80)
        result = guard.execute(self.write_req(new, cpu=0, p1=2, p2=8), self.confirm)
        self.assertEqual(result.response.rc, 0)
        self.assertTrue(result.verified)
        self.assertEqual(result.readback.data, new)
        undo = guard.revert_plan()[-1]
        self.assertEqual((undo.p1, undo.p2, undo.data), (2, 8, RING8))  # the point's baseline word, exactly

    def test_without_an_entry_for_p1_2_the_ring_write_is_refused(self) -> None:
        self.policy = with_ring(self.base_policy, p1=(0,))
        guard = self.make_guard()
        self.assert_refused(guard, self.read_req(0, 0x10, p1=2, p2=8), Refusal.NOT_ALLOWLISTED)
        self.assert_refused(guard, self.write_req(point_word(39, -80), cpu=0, p1=2, p2=8), Refusal.NOT_ALLOWLISTED)

    def test_p1_7_is_refused_even_with_a_wide_entry(self) -> None:
        with self.assertRaises(PolicyError):
            with_ring(self.base_policy, p1=(0, 2, 7))

    def test_p1_7_request_never_reaches_the_check_as_allowed(self) -> None:
        guard = self.open()
        self.assert_refused(guard, self.write_req(point_word(39, -80), cpu=0, p1=7, p2=8), Refusal.NOT_ALLOWLISTED)

    def test_there_is_no_step_limit_on_ring_points(self) -> None:
        guard = self.open()
        result = guard.execute(self.write_req(point_word(39, -400), cpu=0, p1=2, p2=8), self.confirm)  # D-049
        self.assertTrue(result.verified)

    def test_a_ring_write_may_not_change_the_ratio(self) -> None:
        guard = self.open()
        exc = self.assert_refused(guard, self.write_req(point_word(40, -80), cpu=0, p1=2, p2=8), Refusal.CHECK_FAILED)
        self.assertIn("ratio", exc.detail)
