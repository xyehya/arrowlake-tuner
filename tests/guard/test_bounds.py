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
from arltuner.guard.policy import SANITY_OFFSET_CEILING_MV, SANITY_RATIO_MAX, SANITY_V_TARGET_MAX_MV, CheckContext, hw_v_max_raw
from arltuner.mailbox import VfWord

from .fakes import RecordingTransport
from .test_measured_checks import MeasuredCase

E8 = dict(cpu=8, vf=0x0, favored=0x2E30, fused=0x4A02E, granular=0x3, vmax=0x56D)
P1 = dict(cpu=1, vf=0x0, favored=0x3737, fused=0x53839, granular=0x3, vmax=0x681)
E_V = 0x4A0  # the fused V target of E8 (1184 raw, 1.156 V)
P_V = 0x538  # the fused V target of P1 (1336 raw, 1.3047 V)
ALL = ("10", "1C", "09", "26", "28")

#: a reviewed policy that allows a raise: E up to ratio 50 / 1.250 V, P up to ratio 58 / 1.500 V, offsets up to +25 mV (the shipped steps)
RAISE = dict(ratio_max={"E": 50, "P": 58}, v_target_max_mv={"E": 1250, "P": 1500}, offset_ceiling_mv=25)
SHIPPED_STEPS = dict(max_ratio_step=1, max_v_step_mv=20, max_offset_step_mv=25)
RELAXED_STEPS = dict(max_ratio_step=8, max_v_step_mv=200, max_offset_step_mv=200)


def word(ratio: int, v_raw: int, offset_raw: int = 0) -> int:
    return VfWord(ratio, v_raw, False, offset_raw).to_u32()


class BoundsCase(MeasuredCase):
    """A guard session on one measured core. ``policy_changes`` replace Policy fields on top of the fixture's (relaxed steps, 40 ops)."""

    def seed(self, core: dict) -> None:
        super().seed(core)
        self.transport.state[(core["cpu"], 0x28, 0, 0)] = core["vmax"]

    def open(self, core: dict, *, which=ALL, steps=None, **policy_changes):
        """A guard for ``core`` with the reads in ``which`` done. ``steps``: ``SHIPPED_STEPS`` / ``RELAXED_STEPS`` (default relaxed)."""
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


class AboveTheCeilings(BoundsCase):
    def test_ratio_above_the_policy_ceiling_is_refused(self) -> None:
        guard = self.open(E8, **RAISE)
        self.accept(guard, E8, word(50, E_V))
        exc = self.refuse(guard, E8, word(51, E_V), "ratio 51 above 50 (policy ratio_max")
        self.assertIn("ratio_max", exc.detail)

    def test_v_target_above_the_policy_ceiling_is_refused(self) -> None:
        ceiling = VfWord.v_to_raw(1.25)  # 1280
        guard = self.open(E8, **RAISE)
        self.accept(guard, E8, word(48, ceiling))
        self.refuse(guard, E8, word(48, ceiling + 1), "policy v_target_max_mv")

    def test_offset_above_the_policy_ceiling_is_refused(self) -> None:
        guard = self.open(E8, **RAISE)
        self.refuse(guard, E8, word(48, E_V, 27), "outside")  # +25 mV = raw 26 is the ceiling
        self.accept(guard, E8, word(48, E_V, 26))

    def test_a_ceiling_per_core_kind(self) -> None:
        guard = self.open(P1, ratio_max={"E": 50, "P": None}, v_target_max_mv={"E": 1250, "P": None})
        self.refuse(guard, P1, word(56, P_V), "no-raise value")  # P has no ratio number: the default no-raise ceiling (55)
        self.fresh()
        guard = self.open(P1, ratio_max={"E": 50, "P": 56}, v_target_max_mv={"E": 1250, "P": None})
        self.accept(guard, P1, word(56, P_V))
        self.refuse(guard, P1, word(56, P_V + 1), "no-raise value")  # the P V target number is blank: fused is the ceiling

    def test_ceilings_are_judged_before_the_step_limit(self) -> None:
        guard = self.open(E8, steps=SHIPPED_STEPS, **RAISE)
        exc = self.refuse(guard, E8, word(53, E_V), "above 50")  # both too big a step AND above the ceiling: the root cause is named
        self.assertNotIn("per-operation limit", exc.detail)


class TheShippedPolicyCannotOverclock(BoundsCase):
    """No policy numbers -> the ceilings are the no-raise values max(baseline, 0x1C[7:0] / fused voltage): every raise is refused."""

    def test_every_raise_is_refused_and_every_undervolt_allowed(self) -> None:
        policy = default_m1_policy()
        self.assertEqual((dict(policy.ratio_max), dict(policy.v_target_max_mv), policy.offset_ceiling_mv), ({"P": None, "E": None}, {"P": None, "E": None}, 0))
        for core, v in ((E8, E_V), (P1, P_V)):
            ratio = core["favored"] & 0xFF
            with self.subTest(core=core["cpu"]):
                self.fresh()
                guard = self.open(core, steps=SHIPPED_STEPS)
                self.refuse(guard, core, word(ratio + 1, v), "no-raise value")
                self.refuse(guard, core, word(ratio, v + 1), "no-raise value")
                self.refuse(guard, core, word(ratio, v, 1), "outside")  # a positive offset
                self.accept(guard, core, word(ratio, v, -10))  # an undervolting offset is still fine
                self.accept(guard, core, word(ratio - 1, v, -10))  # and so is lowering

    def test_a_policy_cannot_be_raised_by_the_request_itself(self) -> None:
        # a request carries no limits: the only raise is a number in the policy (here: absent), whatever the word says
        guard = self.open(E8)
        for ratio in (49, 60, 255):
            self.refuse(guard, E8, word(ratio, E_V), "ratio")

    def test_an_already_raised_core_may_stay_where_it_is_and_come_down(self) -> None:
        # baseline 0x10 word says ratio 50: the no-raise ceiling is the current ratio (the larger of baseline and 0x1C[7:0])
        guard = self.open(dict(E8, vf=word(50, E_V + 20)), steps=SHIPPED_STEPS)
        self.accept(guard, E8, word(50, E_V + 20, -5))
        self.refuse(guard, E8, word(51, E_V + 20), "no-raise value")
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

    def test_a_v_target_above_the_hardware_maximum_is_refused_even_if_the_policy_allows_it(self) -> None:
        low_vmax = dict(E8, vmax=0x500)  # hardware max 0x4FF = 1279 raw
        guard = self.open(low_vmax, **RAISE)  # policy ceiling 1.250 V = 1280 raw
        self.refuse(guard, low_vmax, word(48, 1280), "hardware maximum")
        self.accept(guard, low_vmax, word(48, 1279))

    def test_the_hardware_maximum_binds_at_exactly_raw_minus_one(self) -> None:
        policy = dict(ratio_max={"E": 60, "P": 62}, v_target_max_mv={"E": 1355, "P": 1625})  # the sanity maxima of this CPU
        for core, top in ((E8, 0x56C), (P1, 0x680)):
            ratio = core["favored"] & 0xFF
            near_top = dict(core, vf=word(ratio, top - 10))  # a current V target close to the maximum, so that the step limit is no issue
            with self.subTest(cpu=core["cpu"]):
                self.fresh()
                guard = self.open(near_top, **policy)
                self.accept(guard, core, word(ratio, top))
                self.refuse(guard, core, word(ratio, top + 1), "above")

    def test_the_0x28_word_of_another_cpu_does_not_count(self) -> None:
        self.seed(E8)
        self.seed(P1)
        self.policy = dataclasses.replace(self.policy, **RELAXED_STEPS, **RAISE)
        guard = self.make_guard()
        self.read_all(guard, E8, ("10", "1C", "09", "26"))
        self.read_all(guard, P1, ("28",))  # CPU 1's 0x28, not CPU 8's
        self.refuse(guard, E8, word(48, E_V + 1), "0x28")


class StepLimits(BoundsCase):
    def test_a_ratio_step_of_one_is_allowed_two_is_not(self) -> None:
        guard = self.open(E8, steps=SHIPPED_STEPS, **RAISE)
        self.refuse(guard, E8, word(50, E_V), "ratio step +2")
        self.accept(guard, E8, word(49, E_V))

    def test_a_v_target_step_is_limited_both_ways(self) -> None:
        guard = self.open(E8, steps=SHIPPED_STEPS, **RAISE)
        limit = VfWord.v_to_raw(0.020)
        self.refuse(guard, E8, word(48, E_V + limit + 1), "V target step")
        self.refuse(guard, E8, word(48, E_V - limit - 1), "V target step")  # lowering too: a big drop is as risky as a big rise
        self.accept(guard, E8, word(48, E_V + limit))

    def test_an_offset_step_is_limited_both_ways(self) -> None:
        guard = self.open(E8, steps=SHIPPED_STEPS, **dict(RAISE, offset_ceiling_mv=50))
        limit = VfWord.mv_to_offset_raw(25)
        self.refuse(guard, E8, word(48, E_V, limit + 1), "offset step")
        self.refuse(guard, E8, word(48, E_V, -limit - 1), "offset step")
        self.accept(guard, E8, word(48, E_V, -limit))

    def test_steps_are_measured_from_the_baseline_not_from_the_last_word(self) -> None:
        # a session cannot walk further than one step from where it started by repeating writes
        guard = self.open(E8, steps=SHIPPED_STEPS, **RAISE)
        self.accept(guard, E8, word(49, E_V))
        self.refuse(guard, E8, word(50, E_V), "ratio step +2")

    def test_the_reference_is_the_xtu_shaped_current_word_when_the_baseline_is_zero(self) -> None:
        # baseline 0x0: the current ratio is 0x1C[7:0] = 48 and the current V target the fused 1184, not 0
        guard = self.open(E8, steps=SHIPPED_STEPS, **RAISE)
        self.accept(guard, E8, word(49, E_V))
        self.fresh()
        guard = self.open(E8, steps=SHIPPED_STEPS, **RAISE)
        self.refuse(guard, E8, word(1, E_V), "ratio step")  # 1 is a legal ratio field; the step from 48 is not

    def test_a_baseline_with_a_v_target_is_its_own_reference(self) -> None:
        guard = self.open(dict(E8, vf=word(49, E_V + 20)), steps=SHIPPED_STEPS, **RAISE)
        self.accept(guard, E8, word(50, E_V + 20))
        self.refuse(guard, E8, word(49, E_V + 41), "V target step")

    def test_the_policy_step_values_are_used(self) -> None:
        steps = dict(max_ratio_step=2, max_v_step_mv=40, max_offset_step_mv=25)
        high = dict(RAISE, ratio_max={"E": 60, "P": 62}, v_target_max_mv={"E": 1355, "P": 1625})  # ceilings out of the way
        guard = self.open(E8, steps=steps, **high)
        self.accept(guard, E8, word(50, E_V))
        self.refuse(guard, E8, word(51, E_V), "ratio step +3")
        self.fresh()
        guard = self.open(E8, steps=steps, **high)
        self.accept(guard, E8, word(48, E_V + VfWord.v_to_raw(0.040)))


class Ordering(BoundsCase):
    def test_a_ratio_and_a_v_target_that_rise_together_are_refused(self) -> None:
        guard = self.open(E8, **RAISE)
        exc = self.refuse(guard, E8, word(49, E_V + 20), "raise the V target first")
        self.assertIn("together", exc.detail)

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

    def test_refusals_send_nothing_and_leave_no_revert(self) -> None:
        guard = self.open(E8)
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
        self.assertEqual((p.max_ratio_step, p.max_v_step_mv, p.max_offset_step_mv, p.offset_ceiling_mv), (1, 20, 25, 0))
        q = load_policy("cpus = [0]\n")
        self.assertEqual((dict(q.ratio_max), dict(q.v_target_max_mv)), ({"P": None, "E": None}, {"P": None, "E": None}))

    def test_a_policy_file_can_set_every_bound(self) -> None:
        q = load_policy(
            "cpus = [0, '8-23']\noffset_ceiling_mv = 20\nmax_ratio_step = 2\nmax_v_step_mv = 30\nmax_offset_step_mv = 40\n"
            "[ratio_max]\nE = 50\nP = 58\n[v_target_max_mv]\nE = 1250\n"
        )
        self.assertEqual(dict(q.ratio_max), {"P": 58, "E": 50})
        self.assertEqual(dict(q.v_target_max_mv), {"P": None, "E": 1250})  # a blank kind keeps the no-raise default
        self.assertEqual((q.offset_ceiling_mv, q.max_ratio_step, q.max_v_step_mv, q.max_offset_step_mv), (20, 2, 30, 40))

    def test_the_tables_are_read_only(self) -> None:
        q = load_policy("cpus = [0]\n[ratio_max]\nE = 50\n")
        with self.assertRaises(TypeError):
            q.ratio_max["E"] = 60  # type: ignore[index]

    def test_absolute_sanity_bounds_are_refused_at_load(self) -> None:
        self.assertEqual((SANITY_V_TARGET_MAX_MV["E"], SANITY_V_TARGET_MAX_MV["P"]), (1355, 1625))
        self.assertEqual((SANITY_RATIO_MAX["P"], SANITY_RATIO_MAX["E"]), (62, 60))
        base = "cpus = [0]\n"
        cases = {
            "E ratio above the sanity bound": (base + "[ratio_max]\nE = 61\n", "ratio_max"),
            "P ratio above the sanity bound": (base + "[ratio_max]\nP = 63\n", "ratio_max"),
            "E ceiling above 1.355 V": (base + "[v_target_max_mv]\nE = 1356\n", "v_target_max_mv"),
            "P ceiling above 1.625 V": (base + "[v_target_max_mv]\nP = 1626\n", "v_target_max_mv"),
            "offset ceiling above the sanity bound": (base + f"offset_ceiling_mv = {SANITY_OFFSET_CEILING_MV + 1}\n", "offset_ceiling_mv"),
            "negative offset ceiling": (base + "offset_ceiling_mv = -1\n", "offset_ceiling_mv"),
            "ratio zero": (base + "[ratio_max]\nE = 0\n", "ratio_max"),
            "voltage below 500 mV": (base + "[v_target_max_mv]\nE = 499\n", "v_target_max_mv"),
            "step zero": (base + "max_ratio_step = 0\n", "max_ratio_step"),
            "ratio step too big": (base + "max_ratio_step = 9\n", "max_ratio_step"),
            "v step too small": (base + "max_v_step_mv = 1\n", "max_v_step_mv"),
            "v step too big": (base + "max_v_step_mv = 201\n", "max_v_step_mv"),
            "offset step too small": (base + "max_offset_step_mv = 1\n", "max_offset_step_mv"),
            "offset step too big": (base + "max_offset_step_mv = 201\n", "max_offset_step_mv"),
        }
        for label, (text, fragment) in cases.items():
            with self.subTest(label):
                with self.assertRaises(PolicyError) as cm:
                    load_policy(text)
                self.assertIn(fragment, str(cm.exception))

    def test_the_sanity_maxima_themselves_are_accepted(self) -> None:
        q = load_policy(
            "cpus = [0]\noffset_ceiling_mv = 200\nmax_ratio_step = 8\nmax_v_step_mv = 200\nmax_offset_step_mv = 200\n"
            "[ratio_max]\nE = 60\nP = 62\n[v_target_max_mv]\nE = 1355\nP = 1625\n"
        )
        self.assertEqual((q.ratio_max["E"], q.v_target_max_mv["P"]), (60, 1625))

    def test_unknown_keys_and_bad_shapes_are_refused(self) -> None:
        cases = {
            "unknown top-level key": "cpus = [0]\nratio_cap = 50\n",
            "unknown kind in ratio_max": "cpus = [0]\n[ratio_max]\nX = 50\n",
            "unknown kind in v_target_max_mv": "cpus = [0]\n[v_target_max_mv]\nG = 1000\n",
            "ratio_max is not a table": "cpus = [0]\nratio_max = 50\n",
            "v_target_max_mv is a list": "cpus = [0]\nv_target_max_mv = [1250]\n",
            "boolean ceiling": "cpus = [0]\n[ratio_max]\nE = true\n",
            "float ceiling": "cpus = [0]\n[v_target_max_mv]\nE = 1250.5\n",
            "string step": "cpus = [0]\nmax_ratio_step = '1'\n",
        }
        for label, text in cases.items():
            with self.subTest(label), self.assertRaises(PolicyError):
                load_policy(text)

    def test_direct_construction_is_validated_too(self) -> None:
        for kwargs in (
            {"ratio_max": {"E": 61, "P": None}},
            {"ratio_max": {"X": 5}},
            {"v_target_max_mv": {"E": 1356}},
            {"v_target_max_mv": 1250},
            {"offset_ceiling_mv": 201},
            {"max_v_step_mv": True},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(PolicyError):
                Policy(cpus=frozenset({0}), **kwargs)

    def test_old_check_name_is_gone(self) -> None:
        # an old policy file naming vf_undervolt is refused loudly instead of being silently reinterpreted
        text = 'cpus=[0]\n[[allow]]\nname="w"\ncmd=0x11\np1=[0]\np2=[0]\nkind="write"\ncheck="vf_undervolt"\n'
        with self.assertRaises(PolicyError) as cm:
            load_policy(text)
        self.assertIn("unknown check", str(cm.exception))

    def test_the_context_defaults_are_the_shipped_policy(self) -> None:
        self.assertEqual(CheckContext(), CheckContext(None, None, None, None, None, None, None, 0, 1, 20, 26))


if __name__ == "__main__":
    unittest.main()
