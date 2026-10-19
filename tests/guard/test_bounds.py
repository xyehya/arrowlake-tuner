"""T-022 guard tests: safety by **bounds, not direction** (D-016) -- the ``vf_bounded`` check, the policy fields behind it, the
voltage-first / frequency-first ordering and the revert plan of a raise.

Measured words (Linux numbering, ``[MEASURED win 2026-10-01]``, win-xtu-live-trace.md S-03d/S-04d/S-06): E core 8 (cluster of CPUs 8-11)
has ``0x10`` = 0x0, ``0x1C`` = 0x2E30 (ratio 48), fused ``0x09`` = 0x4A02E (V target 0x4A0 = 1184 raw = 1.156 V), ``0x28`` = 0x56D; P core 1
has ``0x1C`` = 0x3737 (55), fused 0x53839 (V target 0x538), ``0x28`` = 0x681. Whether the hardware accepts any raise is ``[UNVERIFIED]``: these
tests only prove what the *guard* lets through or refuses, against a recording transport.

Same rules as test_guard.py: every refusal sends nothing, and every test is written so that removing the rule it names makes it fail.
"""
from __future__ import annotations

import dataclasses
import unittest

from arltuner.guard import GuardRefused, Policy, PolicyError, Refusal, default_m1_policy, load_policy
from arltuner.guard.policy import (
    ENCODING_OFFSET_CEILING_MV,
    ENCODING_OFFSET_FLOOR_MV,
    ENCODING_RATIO_MAX,
    ENCODING_V_TARGET_MAX_MV,
    CheckContext,
    hw_v_max_raw,
)
from arltuner.mailbox import VfWord

from .fakes import RecordingTransport
from .test_measured_checks import MeasuredCase

E8 = dict(cpu=8, vf=0x0, favored=0x2E30, fused=0x4A02E, granular=0x3, vmax=0x56D)
P1 = dict(cpu=1, vf=0x0, favored=0x3737, fused=0x53839, granular=0x3, vmax=0x681)
E_V = 0x4A0  # the fused V target of E8 (1184 raw, 1.156 V)
P_V = 0x538  # the fused V target of P1 (1336 raw, 1.3047 V)
ALL = ("10", "1C", "09", "26", "28")

# hw_ratio_max: the CPU's own limit (cmd 0x01, D-039) as the reference machine reports it (0x778 = 120x). D-049: it is the ONLY ratio bound; the voltage limit is the 0x28 word of the session.
CPU_LIMIT = dict(hw_ratio_max={"P": 120, "E": 120})
RAISE = dict(CPU_LIMIT)  # kept for the importers: a raise is allowed within the CPU's limits, no policy number is needed any more
SHIPPED_STEPS: dict = {}  # D-049: no step limits; the names stay so that the importers (test_hw_limits ...) and the `steps=` argument of `open` keep working
RELAXED_STEPS: dict = {}


def word(ratio: int, v_raw: int, offset_raw: int = 0) -> int:
    return VfWord(ratio, v_raw, False, offset_raw).to_u32()


class BoundsCase(MeasuredCase):
    """A guard session on one measured core. ``policy_changes`` replace Policy fields on top of the fixture's (relaxed steps, 40 ops)."""

    def seed(self, core: dict) -> None:
        super().seed(core)
        self.transport.state[(core["cpu"], 0x28, 0, 0)] = core["vmax"]

    def open(self, core: dict, *, which=ALL, steps=None, **policy_changes):
        """A guard for ``core`` with the reads in ``which`` done. ``steps``: ``SHIPPED_STEPS`` / ``RELAXED_STEPS`` (default relaxed)."""
        policy_changes = {**CPU_LIMIT, **policy_changes}
        self.policy = dataclasses.replace(self.policy, **(RELAXED_STEPS if steps is None else steps), **policy_changes)
        guard = self.session(core, which)
        return guard

    def fresh(self) -> None:
        """A new transport, audit log and lock for the next scenario of one test."""
        self._n = getattr(self, "_n", 0) + 1
        self.transport = RecordingTransport()
        self.audit_path = self.dir / f"audit-{self._n}.jsonl"
        self.lock_path = self.dir / f"guard-{self._n}.lock"

    def read_all(self, guard, core: dict, which=ALL) -> None:
        cpu = core["cpu"]
        for name in which:
            cmd = int(name, 16)
            guard.execute(self.read_req(cpu, cmd, p1=0x11 if cmd == 0x09 else 0), self.confirm)

    def session(self, core: dict, which=ALL, **overrides):
        self.seed(core)
        guard = self.make_guard(transport=self.transport, lock_path=self.lock_path, **overrides)
        self.read_all(guard, core, which)
        return guard

    def refuse(self, guard, core: dict, data: int, fragment: str = "") -> GuardRefused:
        exc = self.assert_refused(guard, self.write_req(data, cpu=core["cpu"]), Refusal.CHECK_FAILED)
        self.assertIn(fragment, exc.detail)
        self.assertFalse(guard.tripped)
        return exc

    def accept(self, guard, core: dict, data: int):
        result = self.vf_write(guard, core["cpu"], data)
        self.assertIs(result.verified, True)
        return result


class RaiseWithinTheBounds(BoundsCase):
    def test_e_48_to_49_at_plus_20_mv_goes_voltage_first_and_reads_back(self) -> None:
        guard = self.open(E8, steps=SHIPPED_STEPS, **RAISE)
        v_up = E_V + 20  # +20 raw = +19.5 mV, the step limit of 20 mV (round(20 * 1.024) = 20 raw)
        self.accept(guard, E8, word(48, v_up))  # write 1: the new V target, the OLD ratio
        self.accept(guard, E8, word(49, v_up))  # write 2: the full word
        sent = [c for c in self.transport.calls if c[1] in (0x11, 0x10)][-4:]
        self.assertEqual(
            sent,
            [(8, 0x11, 0, 0, word(48, v_up)), (8, 0x10, 0, 0, 0), (8, 0x11, 0, 0, word(49, v_up)), (8, 0x10, 0, 0, 0)],
        )  # each write is followed by its 0x10 read-back, which the guard compared with the word written
        self.assertEqual(self.transport.state[(8, 0x10, 0, 0)], word(49, v_up))
        self.assertEqual(guard.execute(self.fav_write(8, 49), self.confirm).verified, True)  # 0x1D follows the RAISED ratio
        self.assertEqual(guard.execute(self.gran_write(8, 3), self.confirm).verified, True)
        self.assertFalse(guard.tripped)

    def test_a_raise_is_allowed_on_p_cores_too(self) -> None:
        guard = self.open(P1, steps=SHIPPED_STEPS, **RAISE)
        self.accept(guard, P1, word(55, P_V + 20))
        self.accept(guard, P1, word(56, P_V + 20))

    def test_ratio_only_and_voltage_only_raises_are_single_words(self) -> None:
        for data in (word(49, E_V), word(48, E_V + 20)):
            with self.subTest(data=hex(data)):
                self.fresh()
                guard = self.open(E8, steps=SHIPPED_STEPS, **RAISE)
                self.accept(guard, E8, data)

    def test_a_positive_offset_up_to_the_policy_ceiling(self) -> None:
        guard = self.open(E8, steps=SHIPPED_STEPS, **RAISE)
        self.accept(guard, E8, word(48, E_V, VfWord.mv_to_offset_raw(25)))  # +25 mV = raw 26: the ceiling and the step limit

    def test_lowering_is_just_another_move_inside_the_bounds(self) -> None:
        guard = self.open(E8, steps=SHIPPED_STEPS)  # the DEFAULT policy
        self.accept(guard, E8, word(47, E_V, -20))


class AboveTheCpuLimits(BoundsCase):
    """D-049: the only ceilings are the CPU's own (cmd 0x01 [7:0] ratio, cmd 0x28 [11:0] - 1 LSB voltage), refused at the CPU value with the register cited."""

    def test_ratio_above_the_cpu_limit_is_refused(self) -> None:
        guard = self.open(E8, hw_ratio_max={"E": 50, "P": 50})
        self.accept(guard, E8, word(50, E_V))
        exc = self.refuse(guard, E8, word(51, E_V), "ratio 51 above the CPU limit 50x")
        self.assertIn("cmd 0x01", exc.detail)

    def test_v_target_above_the_cpu_limit_is_refused_at_raw_minus_one(self) -> None:
        guard = self.open(E8)  # the session read 0x28 = 0x56D: limit 0x56C
        self.accept(guard, E8, word(48, 0x56C))
        exc = self.refuse(guard, E8, word(48, 0x56C + 1), "CPU limit")
        self.assertIn("cmd 0x28", exc.detail)

    def test_offset_above_the_cpu_voltage_limit_is_refused(self) -> None:
        guard = self.open(E8)
        over = 0x56C - E_V  # E_V + over = the limit
        self.refuse(guard, E8, word(48, E_V, over + 1), "predicted voltage")
        self.accept(guard, E8, word(48, E_V, over))

    def test_a_limit_per_core_kind(self) -> None:
        guard = self.open(P1, hw_ratio_max={"E": 50, "P": 56})
        self.accept(guard, P1, word(56, P_V))
        self.refuse(guard, P1, word(57, P_V), "56x")
        self.fresh()
        guard = self.open(E8, hw_ratio_max={"E": 49, "P": 120})
        self.refuse(guard, E8, word(50, E_V), "49x")  # E uses the E limit, not the P one

    def test_big_changes_are_not_refused(self) -> None:
        # D-049: no step limit: +10 bins, a 100 mV offset, a 150 mV V target move each in ONE word
        guard = self.open(E8, **RAISE)
        self.accept(guard, E8, word(58, E_V))
        self.accept(guard, E8, word(58, E_V, -102))
        self.accept(guard, E8, word(58, E_V + 100, -102))


class WithoutTheCpuLimits(BoundsCase):
    """The CPU's limits not read -> a raise is refused naming the read (``CPU_LIMIT_UNREAD``); lowering and staying put are not raises."""

    UNREAD = dict(hw_ratio_max={"P": None, "E": None})

    def test_every_raise_is_refused_and_every_undervolt_allowed(self) -> None:
        policy = default_m1_policy()
        self.assertEqual(dict(policy.hw_ratio_max), {"P": None, "E": None})
        for core, v in ((E8, E_V), (P1, P_V)):
            ratio = core["favored"] & 0xFF
            with self.subTest(core=core["cpu"]):
                self.fresh()
                guard = self.open(core, which=("10", "1C", "09", "26"), **self.UNREAD)  # no 0x28 either
                self.refuse(guard, core, word(ratio + 1, v), "CPU_LIMIT_UNREAD")
                self.refuse(guard, core, word(ratio, v + 1), "CPU_LIMIT_UNREAD")
                self.refuse(guard, core, word(ratio, v, 1), "CPU_LIMIT_UNREAD")  # a positive offset raises the predicted voltage
                self.accept(guard, core, word(ratio, v, -10))  # an undervolting offset is still fine
                self.accept(guard, core, word(ratio - 1, v, -10))  # and so is lowering

    def test_the_refusal_names_the_read(self) -> None:
        guard = self.open(E8, which=("10", "1C", "09", "26"), **self.UNREAD)
        self.refuse(guard, E8, word(49, E_V), "cmd 0x01")
        self.refuse(guard, E8, word(48, E_V + 1), "cmd 0x28")

    def test_a_request_cannot_raise_the_cpu_limit(self) -> None:
        # a request carries no limits: with the limit unread every raise is refused, whatever the word says
        guard = self.open(E8, **self.UNREAD)
        for ratio in (49, 60, 255):
            self.refuse(guard, E8, word(ratio, E_V), "ratio")

    def test_an_already_raised_core_may_stay_where_it_is_and_come_down(self) -> None:
        # baseline 0x10 word says ratio 50: the current ratio is the larger of baseline and 0x1C[7:0]
        guard = self.open(dict(E8, vf=word(50, E_V + 20)), **self.UNREAD)
        self.accept(guard, E8, word(50, E_V + 20, -5))
        self.refuse(guard, E8, word(51, E_V + 20), "CPU_LIMIT_UNREAD")
        self.accept(guard, E8, word(49, E_V + 20))


class HardwareMaximumVoltage(BoundsCase):
    def test_hw_v_max_is_the_0x28_field_minus_one_and_0_means_2_volts(self) -> None:
        self.assertEqual(hw_v_max_raw(0x56D), 0x56C)  # E: 1388 raw = 1.3555 V
        self.assertEqual(hw_v_max_raw(0x681), 0x680)  # P: 1664 raw = 1.625 V
        self.assertEqual(hw_v_max_raw(0xF000_056D), 0x56C)  # only [11:0]
        self.assertEqual(hw_v_max_raw(0), 2048)  # registers.md: 0 => 2.0 V

    def test_a_raise_without_the_0x28_read_is_refused_fail_closed(self) -> None:
        guard = self.open(E8, which=("10", "1C", "09", "26"), **RAISE)
        self.refuse(guard, E8, word(48, E_V + 1), "0x28")
        self.accept(guard, E8, word(48, E_V))  # not raising the V target needs no hardware maximum

    def test_a_v_target_above_the_hardware_maximum_is_refused(self) -> None:
        low_vmax = dict(E8, vmax=0x500)  # hardware max 0x4FF = 1279 raw
        guard = self.open(low_vmax, **RAISE)
        self.refuse(guard, low_vmax, word(48, 1280), "CPU limit")
        self.accept(guard, low_vmax, word(48, 1279))

    def test_the_hardware_maximum_binds_at_exactly_raw_minus_one(self) -> None:
        for core, top in ((E8, 0x56C), (P1, 0x680)):
            ratio = core["favored"] & 0xFF
            near_top = dict(core, vf=word(ratio, top - 10))
            with self.subTest(cpu=core["cpu"]):
                self.fresh()
                guard = self.open(near_top)
                self.accept(guard, core, word(ratio, top))
                self.refuse(guard, core, word(ratio, top + 1), "above")

    def test_field_0_of_0x28_is_a_2_volt_limit_not_an_unread_one(self) -> None:
        zero = dict(E8, vmax=0)  # a word that WAS read as 0
        guard = self.open(zero)
        self.accept(guard, zero, word(48, 2048))
        self.refuse(guard, zero, word(48, 2049), "CPU limit")

    def test_the_0x28_word_of_another_cpu_does_not_count(self) -> None:
        self.seed(E8)
        self.seed(P1)
        self.policy = dataclasses.replace(self.policy, **RELAXED_STEPS, **RAISE)
        guard = self.make_guard()
        self.read_all(guard, E8, ("10", "1C", "09", "26"))
        self.read_all(guard, P1, ("28",))  # CPU 1's 0x28, not CPU 8's
        self.refuse(guard, E8, word(48, E_V + 1), "0x28")


class NoStepLimits(BoundsCase):
    """D-049: the per-operation step limits are gone. Any change inside the CPU's limits goes in ONE word; the ordering rule is the only thing that splits a move."""

    def test_a_ratio_jump_is_one_word(self) -> None:
        guard = self.open(E8, **RAISE)
        self.accept(guard, E8, word(50, E_V))
        self.accept(guard, E8, word(60, E_V))

    def test_a_v_target_jump_is_one_word_both_ways(self) -> None:
        guard = self.open(E8, **RAISE)
        self.accept(guard, E8, word(48, E_V + 150))
        self.accept(guard, E8, word(48, E_V - 150))

    def test_an_offset_jump_is_one_word_both_ways(self) -> None:
        guard = self.open(E8, **RAISE)
        self.accept(guard, E8, word(48, E_V, 100))
        self.accept(guard, E8, word(48, E_V, -500))
        self.accept(guard, E8, word(48, E_V, -1024))

    def test_a_cold_word_is_no_longer_judged_against_a_ratio_step_from_the_fill(self) -> None:
        # baseline 0x0: the current ratio is 0x1C[7:0] = 48; 1 is a legal ratio field and a lowering
        guard = self.open(E8, **RAISE)
        self.accept(guard, E8, word(1, E_V))

    def test_a_baseline_with_a_v_target_is_its_own_reference_for_the_raise_check(self) -> None:
        guard = self.open(dict(E8, vf=word(49, E_V + 20)), hw_ratio_max={"E": 49, "P": 49})
        self.refuse(guard, E8, word(50, E_V + 20), "49x")
        self.accept(guard, E8, word(49, E_V + 20, -5))


class Ordering(BoundsCase):
    def test_a_ratio_and_a_v_target_that_rise_together_are_refused(self) -> None:
        guard = self.open(E8, **RAISE)
        exc = self.refuse(guard, E8, word(49, E_V + 20), "raise the V target first")
        self.assertIn("together", exc.detail)

    def test_g241_a_word_that_creates_the_oc_point_carries_ratio_and_voltage_together(self) -> None:
        # BIOS defaults (live 2026-10-19): E max 46 = the top fused ratio, no OC point; the creating word may raise both (the ratio is unusable until 0x1D follows)
        e46 = dict(E8, vf=0x2E, favored=0x2E2E)
        guard = self.open(e46, **RAISE)
        self.accept(guard, E8, word(50, E_V + 30))
        self.refuse(guard, E8, word(51, E_V + 40), "raise the V target first")  # the OC point now exists: the ordering rule is back

    def test_g241_an_existing_oc_point_still_needs_the_voltage_first(self) -> None:
        guard = self.open(dict(E8, vf=word(48, E_V), favored=0x2E30), **RAISE)
        self.refuse(guard, E8, word(50, E_V + 30), "raise the V target first")

    def test_voltage_first_then_ratio_is_accepted_in_that_order_only(self) -> None:
        guard = self.open(E8, **RAISE)
        self.accept(guard, E8, word(48, E_V + 20))  # V first
        self.accept(guard, E8, word(49, E_V + 20))  # then the ratio: relative to the last word sent only the ratio moves
        self.fresh()
        guard = self.open(E8, **RAISE)
        self.accept(guard, E8, word(49, E_V))  # ratio first (V unchanged) is a ratio-only word: allowed
        self.refuse(guard, E8, word(50, E_V + 20), "raise the V target first")  # now both would rise relative to the last word

    def test_a_ratio_and_a_v_target_that_fall_together_are_refused(self) -> None:
        guard = self.open(E8, **RAISE)
        exc = self.refuse(guard, E8, word(47, E_V - 20), "lower the ratio first")
        self.assertIn("together", exc.detail)

    def test_frequency_first_then_voltage_is_accepted(self) -> None:
        guard = self.open(E8, **RAISE)
        self.accept(guard, E8, word(47, E_V))  # ratio first
        self.accept(guard, E8, word(47, E_V - 20))  # then V: only V moves relative to the last word

    def test_opposite_directions_in_one_word_are_not_an_ordering_problem(self) -> None:
        guard = self.open(E8, **RAISE)
        self.accept(guard, E8, word(49, E_V - 20))  # ratio up, V down: the safe order does not exist and is not demanded

    def test_the_rule_looks_at_the_last_word_sent_to_that_cpu_only(self) -> None:
        self.seed(E8)
        self.seed(P1)
        self.policy = dataclasses.replace(self.policy, **RELAXED_STEPS, **RAISE)
        guard = self.make_guard()
        self.read_all(guard, E8)
        self.read_all(guard, P1)
        self.accept(guard, P1, word(55, P_V + 20))  # a V-first word on CPU 1 does not make CPU 8's both-up word legal
        self.refuse(guard, E8, word(49, E_V + 20), "raise the V target first")


class Fundamentals(BoundsCase):
    def test_static_mode_stays_refused_even_inside_every_bound(self) -> None:
        guard = self.open(E8, **RAISE)
        self.refuse(guard, E8, VfWord(48, E_V, True, 0).to_u32(), "static")
        self.fresh()
        guard = self.open(dict(E8, vf=VfWord(48, E_V, True, 0).to_u32()), **RAISE)
        self.refuse(guard, E8, word(48, E_V), "static")  # a static baseline cannot be bounded

    def test_a_zero_ratio_or_a_zero_v_target_is_refused(self) -> None:
        guard = self.open(E8, **RAISE)
        self.refuse(guard, E8, word(0, E_V), "ratio 0")
        self.refuse(guard, E8, word(48, 0), "v_target_raw 0")

    def test_a_zero_v_target_is_allowed_only_without_an_oc_point(self) -> None:
        """G-120 (D-035): ratio <= the top fused ratio (E 46, P 57) = no OC point = ratio | offset << 21 with target 0 [MEASURED lnx 2026-10-11]."""
        guard = self.open(E8)
        self.accept(guard, E8, word(46, 0, -10))  # E top fused ratio: no OC point, whole-curve offset
        self.refuse(guard, E8, word(47, 0, -10), "v_target_raw 0")  # above the top fused ratio an OC point needs a real target
        self.refuse(guard, E8, word(0, 0, -10), "ratio 0")
        self.accept(guard, E8, word(46, 0, -200))  # D-049: no offset floor any more
        self.fresh()
        guard = self.open(P1, **RAISE)
        self.accept(guard, P1, word(55, 0, -10))  # P1's 0x1C ratio; the top fused ratio of a P core is 57
        self.refuse(guard, P1, word(58, 0, -10), "v_target_raw 0")

    def test_the_zero_target_exception_needs_the_top_fused_ratio(self) -> None:
        from arltuner.guard.policy import _check_vf_bounded

        req = type("R", (), {"p2": 0, "data": word(46, 0, -10)})()
        ok, why = _check_vf_bounded(req, 0x2E, {}, CheckContext(bound=True, favored_word=0x2E30, fused_word=0x4A02E))
        self.assertFalse(ok)
        self.assertIn("v_target_raw 0", why)

    def test_a_cold_word_at_or_below_the_top_fused_ratio_is_no_ratio_raise(self) -> None:
        """G-122 / D-049: baseline ratio field 0 (a cold E word; 0x1C[7:0] = 48) written at 46 (target 0, no OC point) is a lowering; a ratio above 0x1C[7:0] is a raise that needs the
        CPU's cmd 0x01 limit (unread here = refused, ``CPU_LIMIT_UNREAD``); with the limit bound it only has to fit it."""
        from arltuner.guard.policy import _check_vf_bounded

        def check(base: int, data: int, top: int | None = 46, favored: int = 0x2E30, limit: int | None = None):
            req = type("R", (), {"p2": 0, "data": data})()
            return _check_vf_bounded(req, base, {}, CheckContext(bound=True, favored_word=favored, fused_word=0x4A02E, top_fused_ratio=top, hw_ratio_max=limit, ratio_text="CPU_LIMIT_UNREAD: cmd 0x01 not read"))

        self.assertTrue(check(0x0, word(46, 0, -10))[0])  # cold E word: 0 -> 46, no OC point
        self.assertTrue(check(0x0, word(46, E_V, 0))[0])  # the offset-0 word keeps the filled target
        self.assertTrue(check(0x0, word(44, 0, -10))[0])  # no step limit: 4 below is fine
        self.assertTrue(check(word(48, E_V), word(46, 0, -10))[0])  # a warm baseline at 48 (an OC point): 48 -> 46 is a lowering
        self.assertTrue(check(0x0, word(55, 0, -10), top=57, favored=0x3737)[0])  # P: 0x1C[7:0] = 55 <= top, unchanged
        ok, why = check(0x0, word(57, 0, -10), top=57, favored=0x3737)  # past 0x1C[7:0], limit unread
        self.assertFalse(ok)
        self.assertIn("CPU_LIMIT_UNREAD", why)
        self.assertTrue(check(0x0, word(57, 0, -10), top=57, favored=0x3737, limit=120)[0])
        self.assertFalse(check(0x0, word(57, 0, -10), top=57, favored=0x3737, limit=56)[0])

    def test_refusals_send_nothing_and_leave_no_revert(self) -> None:
        guard = self.open(E8, which=("10", "1C", "09", "26"), hw_ratio_max={"P": None, "E": None})  # the CPU limits unread: each of these is a raise
        before = list(self.transport.calls)
        for data in (word(49, E_V), word(48, E_V + 1), word(48, E_V, 5)):
            self.refuse(guard, E8, data)
        self.assertEqual(self.transport.calls, before)
        self.assertEqual(guard.revert_plan(), [])


class RevertOfARaise(BoundsCase):
    def apply_raise(self, guard) -> None:
        self.accept(guard, E8, word(48, E_V + 20))
        self.accept(guard, E8, word(49, E_V + 20))
        guard.execute(self.fav_write(8, 49), self.confirm)
        guard.execute(self.gran_write(8, 3), self.confirm)

    def test_the_plan_undoes_the_raise_frequency_first_to_the_xtu_shaped_word(self) -> None:
        guard = self.open(E8, steps=SHIPPED_STEPS, **RAISE)
        self.apply_raise(guard)
        plan = guard.revert_plan()
        self.assertEqual(
            [(r.cmd, r.data) for r in plan],
            [
                (0x11, word(48, E_V + 20)),  # the ratio goes back first: the word the V-first write left
                (0x11, word(48, E_V)),  # then the V target: XTU-shaped (ratio 0x1C[7:0], fused V), NOT the zero baseline 0x0
                (0x1D, 48),  # the ratio of the LAST 0x11 undo
                (0x27, 3),
            ],
        )
        self.assertTrue(all(r.purpose == "revert" for r in plan))
        for r in plan:
            fields = VfWord.from_u32(r.data) if r.cmd == 0x11 else None
            if fields is not None:
                self.assertTrue(fields.ratio != 0 and fields.v_target_raw != 0)  # never a word with a zero field

    def test_every_revert_request_passes_the_checks_in_a_new_session_in_that_order(self) -> None:
        guard = self.open(E8, steps=SHIPPED_STEPS, **RAISE)
        self.apply_raise(guard)
        plan = guard.revert_plan()
        # a new session starting from the raised state: reads 0x10 (now the raised word), 0x1C, 0x09, 0x26, 0x28
        self.fresh()
        self.seed(E8)
        self.transport.state[(8, 0x10, 0, 0)] = word(49, E_V + 20)
        self.transport.state[(8, 0x1C, 0, 0)] = 0x2E31
        self.audit_path = self.dir / "second.jsonl"
        second = self.make_guard(transport=self.transport, lock_path=self.dir / "second.lock")
        self.read_all(second, E8)
        for req in plan:
            self.assertIs(second.execute(req, self.confirm).verified, True, req)
        self.assertEqual(self.transport.state[(8, 0x10, 0, 0)], word(48, E_V))
        self.assertFalse(second.tripped)

    def test_the_revert_of_a_ratio_that_fell_with_the_voltage_is_voltage_first(self) -> None:
        guard = self.open(E8, steps=SHIPPED_STEPS, **RAISE)
        self.accept(guard, E8, word(47, E_V))  # frequency first
        self.accept(guard, E8, word(47, E_V - 20))
        plan = [(r.cmd, r.data) for r in guard.revert_plan() if r.cmd == 0x11]
        self.assertEqual(plan, [(0x11, word(47, E_V)), (0x11, word(48, E_V))])  # V back first, then the ratio

    def test_a_reread_baseline_starts_a_new_chain(self) -> None:
        guard = self.open(E8, **RAISE)
        self.accept(guard, E8, word(49, E_V))
        # the operator looks again and finds a word that is NOT the one this session wrote (something else moved the offset): that word is
        # the new baseline, so the next write is undone to IT, not to the previous write of this session
        found = word(49, E_V, -5)
        self.transport.state[(8, 0x10, 0, 0)] = found
        guard.execute(self.read_req(8, 0x10), self.confirm)
        self.accept(guard, E8, word(50, E_V, -5))
        self.assertEqual([r.data for r in guard.revert_plan() if r.cmd == 0x11], [found, word(48, E_V)])

    def test_the_0x1d_undo_carries_the_ratio_of_the_final_0x11_undo(self) -> None:
        # two ratio-only writes in one chain: the undos are 49 then 48, the 0x1D check wants the ratio of the LAST 0x11 sent -> 48
        guard = self.open(E8, **RAISE)
        self.accept(guard, E8, word(49, E_V))
        self.accept(guard, E8, word(50, E_V))
        self.assertIs(guard.execute(self.fav_write(8, 50), self.confirm).verified, True)
        self.assertEqual(
            [(r.cmd, r.data) for r in guard.revert_plan()],
            [(0x11, word(49, E_V)), (0x11, word(48, E_V)), (0x1D, 48)],
        )

    def test_the_xtu_follow_ups_are_undone_after_all_the_0x11_words(self) -> None:
        guard = self.open(E8, steps=SHIPPED_STEPS, **RAISE)
        self.apply_raise(guard)
        self.assertEqual([r.cmd for r in guard.revert_plan()], [0x11, 0x11, 0x1D, 0x27])


class PolicyBounds(unittest.TestCase):
    def test_defaults(self) -> None:
        p = default_m1_policy()
        q = load_policy("cpus = [0]\n")
        for policy in (p, q):
            self.assertEqual((dict(policy.hw_ratio_max), dict(policy.hw_v_target_max_mv)), ({"P": None, "E": None}, {"P": None, "E": None}))  # the CPU's limits start unread
            self.assertEqual(policy.ignored_keys, ())

    def test_a_policy_file_with_the_old_bounds_loads_but_sets_nothing(self) -> None:
        q = load_policy(
            "cpus = [0, '8-23']\noffset_ceiling_mv = 20\nmax_ratio_step = 2\nmax_v_step_mv = 30\nmax_offset_step_mv = 40\n"
            "[ratio_max]\nE = 50\nP = 58\n[v_target_max_mv]\nE = 1250\n"
        )
        self.assertEqual(dict(q.ratio_max), {"P": None, "E": None})  # the neutral stand-ins: the old numbers are NOT in force
        self.assertEqual(dict(q.v_target_max_mv), {"P": None, "E": None})
        self.assertEqual((q.offset_ceiling_mv, q.max_ratio_step, q.max_v_step_mv, q.max_offset_step_mv), (999, 255, 1000, 2000))
        self.assertEqual(dict(q.hw_ratio_max), {"P": None, "E": None})
        self.assertEqual(set(q.ignored_keys), {"offset_ceiling_mv", "max_ratio_step", "max_v_step_mv", "max_offset_step_mv", "ratio_max", "v_target_max_mv"})

    def test_the_cpu_limit_tables_are_read_only(self) -> None:
        q = default_m1_policy()
        with self.assertRaises(TypeError):
            q.hw_ratio_max["E"] = 60  # type: ignore[index]

    def test_field_encoding_bounds_of_the_cpu_limits(self) -> None:
        """D-039: only what the word cannot carry is refused; the CPU limits bound to a policy are checked against the field they come from."""
        self.assertEqual((ENCODING_RATIO_MAX, ENCODING_V_TARGET_MAX_MV), (255, 3999))
        self.assertEqual((ENCODING_OFFSET_FLOOR_MV, ENCODING_OFFSET_CEILING_MV), (-1000, 999))
        for kwargs in (
            {"hw_ratio_max": {"E": ENCODING_RATIO_MAX + 1, "P": None}},
            {"hw_ratio_max": {"E": 0}},
            {"hw_ratio_max": {"X": 5}},
            {"hw_v_target_max_mv": {"E": ENCODING_V_TARGET_MAX_MV + 1}},
            {"hw_v_target_max_mv": 1250},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(PolicyError):
                Policy(cpus=frozenset({0}), **kwargs)

    def test_the_removed_fields_cannot_be_constructed(self) -> None:
        for kwargs in ({"ratio_max": {"E": 50}}, {"v_target_max_mv": {"E": 1250}}, {"offset_ceiling_mv": 0}, {"max_ratio_step": 1}, {"assumed_bios_offset_mv": {"P": 0, "E": 0}}):
            with self.subTest(kwargs=kwargs), self.assertRaises(TypeError):
                Policy(cpus=frozenset({0}), **kwargs)  # type: ignore[arg-type]

    def test_unknown_top_level_keys_are_still_refused(self) -> None:
        for text in ("cpus = [0]\nratio_cap = 50\n", "cpus = [0]\n[ratio_cap]\nE = 50\n"):
            with self.subTest(text=text), self.assertRaises(PolicyError):
                load_policy(text)

    def test_old_check_name_is_gone(self) -> None:
        # an old policy file naming vf_undervolt is refused loudly instead of being silently reinterpreted
        text = 'cpus=[0]\n[[allow]]\nname="w"\ncmd=0x11\np1=[0]\np2=[0]\nkind="write"\ncheck="vf_undervolt"\n'
        with self.assertRaises(PolicyError) as cm:
            load_policy(text)
        self.assertIn("unknown check", str(cm.exception))

    def test_the_context_is_unbound_by_default(self) -> None:
        c = CheckContext()
        self.assertFalse(c.bound)  # only build_context binds: the explicit "there is a context" flag that replaced the offset-floor sentinel
        self.assertEqual((c.hw_ratio_max, c.hw_v_max_raw), (None, None))


if __name__ == "__main__":
    unittest.main()
