"""T-021 guard tests: the checks and rules that follow XTU's *measured* per-core Apply (``[MEASURED win 2026-10-01]``,
docs/knowledge/registers.md level-4 log section, S-04...S-07): session facts for ``0x11`` (``0x1C`` / ``0x09`` reads, zero fields), the ``0x1D`` and
``0x27`` checks and their read-back predicates, the XTU-shaped ``revert_plan``, and the D-015 effective-offset floor in ``Policy``.

Same rules as test_guard.py: every refusal sends nothing (``assert_refused`` checks the recording transport and the audit log), and each
test is written so that removing the behaviour it names makes it fail. The recording transport only models the read/write twin words.
"""
from __future__ import annotations

import dataclasses
import unittest
from types import SimpleNamespace

from arltuner.guard import CHECKS, GuardRefused, Policy, PolicyError, Refusal, Request, default_m1_policy, load_policy
from arltuner.guard.policy import CheckContext, build_context
from arltuner.mailbox import VfWord

from .fakes import RecordingTransport
from .test_guard import GuardCase

# measured words (Linux numbering): S-03d / S-04d / S-06
CPU1 = dict(cpu=1, vf=0x0, favored=0x3737, fused=0x53839, granular=0x3)  # a P core whose baseline word is 0x0
CPU0 = dict(cpu=0, vf=0x39, favored=0x3939, fused=0x5B439, granular=0x3)  # the one core with a baseline word
CPU8 = dict(cpu=8, vf=0x0, favored=0x2E30, fused=0x4A02E, granular=0x3)  # E core: 0x1C[7:0] = 48 > [15:8] = 46


def ctx(**kw) -> CheckContext:
    return CheckContext(**{"bound": True, **kw})


class MeasuredCase(GuardCase):
    def setUp(self) -> None:
        super().setUp()
        self.policy = dataclasses.replace(self.policy, max_operations_per_session=40, max_operations_per_boot=400)

    def seed(self, core: dict) -> None:
        cpu = core["cpu"]
        self.transport.state[(cpu, 0x10, 0, 0)] = core["vf"]
        self.transport.state[(cpu, 0x1C, 0, 0)] = core["favored"]
        self.transport.state[(cpu, 0x09, 0x11, 0)] = core["fused"]
        self.transport.state[(cpu, 0x26, 0, 0)] = core["granular"]

    def read_all(self, guard, core: dict, which=("10", "1C", "09", "26")) -> None:
        cpu = core["cpu"]
        for name in which:
            cmd = int(name, 16)
            guard.execute(self.read_req(cpu, cmd, p1=0x11 if cmd == 0x09 else 0), self.confirm)

    def session(self, core: dict, which=("10", "1C", "09", "26"), **overrides):
        self.seed(core)
        guard = self.make_guard(**overrides)
        self.read_all(guard, core, which)
        return guard

    def vf_write(self, guard, cpu: int, word: int):
        return guard.execute(self.write_req(word, cpu=cpu, cmd=0x11), self.confirm)

    def fav_write(self, cpu: int, data: int) -> Request:
        return self.write_req(data, cpu=cpu, cmd=0x1D)

    def gran_write(self, cpu: int, data: int) -> Request:
        return self.write_req(data, cpu=cpu, cmd=0x27)


class VfWithSessionFacts(MeasuredCase):
    def test_xtus_word_for_a_zero_baseline_core_is_accepted(self) -> None:
        guard = self.session(CPU1)
        result = self.vf_write(guard, 1, 0xFF653837)  # S-04d: ratio 0x37 (0x1C), V target 0x538 (0x09), offset -5
        self.assertIs(result.verified, True)
        self.assertEqual(self.transport.calls[-2], (1, 0x11, 0, 0, 0xFF653837))

    def test_the_same_word_is_refused_when_the_session_has_not_read_0x1c_and_0x09(self) -> None:
        for which in (("10",), ("10", "1C"), ("10", "09")):
            with self.subTest(read=which):
                self.transport = RecordingTransport()
                guard = self.session(CPU1, which, transport=self.transport, lock_path=self.dir / f"{'-'.join(which)}.lock")
                n = len(self.transport.calls)
                exc = self.assert_refused(guard, self.write_req(0xFF653837, cpu=1), Refusal.CHECK_FAILED)
                self.assertIn("no ratio reference" if "1C" not in which else "no voltage reference", exc.detail)
                self.assertEqual(len(self.transport.calls), n)

    def test_a_full_baseline_without_facts_is_its_own_reference_for_the_unread_limits(self) -> None:
        # a core whose 0x10 word is complete is its own reference; with the CPU's limits unread nothing above it may be raised (the planner always reads the facts)
        self.policy = dataclasses.replace(self.policy, hw_ratio_max={"P": None, "E": None})
        word = VfWord(55, 0x4A0, False, 0)
        self.transport.state[(0, 0x10, 0, 0)] = word.to_u32()
        guard = self.make_guard()
        guard.execute(self.read_req(), self.confirm)
        self.assertIs(self.vf_write(guard, 0, word.with_(offset_raw=-20).to_u32()).verified, True)
        self.assert_refused(guard, self.write_req(word.with_(ratio=56).to_u32()), Refusal.CHECK_FAILED)
        self.assert_refused(guard, self.write_req(word.with_(v_target_raw=0x4A1).to_u32()), Refusal.CHECK_FAILED)

    def test_a_zero_ratio_or_v_target_is_refused_even_with_every_reference(self) -> None:
        guard = self.session(CPU0)
        for label, word in (
            ("ratio 0", VfWord(0, 0x5B4, False, -5)),
            ("V target 0 above the top fused ratio", VfWord(0x3A, 0, False, -5)),  # G-120: at or below 57 (no OC point) a zero target is allowed, see test_bounds
            ("both 0", VfWord(0, 0, False, -5)),
        ):
            with self.subTest(label):
                exc = self.assert_refused(guard, self.write_req(word.to_u32(), cpu=0), Refusal.CHECK_FAILED)
                self.assertIn(" 0 ", exc.detail)
        self.assertEqual(guard.revert_plan(), [])

    def test_no_raise_ratio_is_the_larger_of_baseline_and_0x1c_low_byte_while_the_cpu_limit_is_unread(self) -> None:
        # D-016 / D-049: with cmd 0x01 unread the no-raise value is max(baseline ratio, 0x1C[7:0]); a lower baseline ratio does not tighten it
        self.policy = dataclasses.replace(self.policy, hw_ratio_max={"P": None, "E": None})
        guard = self.session(dict(CPU1, vf=0x32))  # baseline ratio 50 < 0x1C[7:0] 55
        self.assert_refused(guard, self.write_req(VfWord(56, 0x538, False, -5).to_u32(), cpu=1), Refusal.CHECK_FAILED)
        self.assertIs(self.vf_write(guard, 1, VfWord(55, 0x538, False, -5).to_u32()).verified, True)
        self.transport = RecordingTransport()
        guard = self.session(CPU1, transport=self.transport, lock_path=self.dir / "b.lock")  # baseline 0: the 0x1C low byte alone
        self.assert_refused(guard, self.write_req(VfWord(56, 0x538, False, -5).to_u32(), cpu=1), Refusal.CHECK_FAILED)
        self.assertIs(self.vf_write(guard, 1, VfWord(55, 0x538, False, -5).to_u32()).verified, True)

    def test_no_raise_v_target_is_the_larger_of_baseline_and_the_fused_voltage_while_cmd_0x28_is_unread(self) -> None:
        guard = self.session(dict(CPU1, vf=0x50037))  # baseline V target 0x500 < fused 0x538
        self.assert_refused(guard, self.write_req(VfWord(55, 0x539, False, -5).to_u32(), cpu=1), Refusal.CHECK_FAILED)
        self.assertIs(self.vf_write(guard, 1, VfWord(55, 0x538, False, -5).to_u32()).verified, True)
        self.transport = RecordingTransport()
        guard = self.session(CPU1, transport=self.transport, lock_path=self.dir / "b.lock")  # baseline 0: the fused voltage alone
        self.assert_refused(guard, self.write_req(VfWord(55, 0x539, False, -5).to_u32(), cpu=1), Refusal.CHECK_FAILED)
        self.assertIs(self.vf_write(guard, 1, VfWord(55, 0x538, False, -5).to_u32()).verified, True)

    def test_the_fused_ratio_of_0x1c_high_byte_is_not_a_ceiling_on_an_e_core(self) -> None:
        guard = self.session(CPU8)
        # XTU wrote ratio 48 on the E cores although 0x1C[15:8] says 46: [7:0] is the reference
        self.assertIs(self.vf_write(guard, 8, 0xFF64A030).verified, True)

    def test_static_mode_baseline_still_cannot_be_evaluated(self) -> None:
        guard = self.session(dict(CPU1, vf=0x100037))  # static bit set in the baseline
        self.assert_refused(guard, self.write_req(0xFF653837, cpu=1), Refusal.CHECK_FAILED)

    def test_facts_of_another_cpu_do_not_count(self) -> None:
        self.seed(CPU1)
        self.seed(CPU0)
        guard = self.make_guard()
        self.read_all(guard, CPU0)
        self.read_all(guard, CPU1, which=("10",))  # CPU 1: baseline only
        self.assert_refused(guard, self.write_req(0xFF653837, cpu=1), Refusal.CHECK_FAILED)

    def test_a_failed_fact_read_is_not_a_fact(self) -> None:
        self.seed(CPU1)
        guard = self.make_guard()
        guard.execute(self.read_req(1, 0x10), self.confirm)
        self.transport.read_rc = 0x5
        guard.execute(self.read_req(1, 0x1C), self.confirm)
        guard.execute(self.read_req(1, 0x09, p1=0x11), self.confirm)
        self.transport.read_rc = 0
        self.assert_refused(guard, self.write_req(0xFF653837, cpu=1), Refusal.CHECK_FAILED)


class OffsetField(MeasuredCase):
    """D-049 (replaces the D-015 effective-offset floor): the offset is bounded by the 11-bit field, the entry's optional params and, upward, by the CPU's voltage limit."""

    def accepted(self, core: dict, raw: int) -> bool:
        self.transport = RecordingTransport()
        self.audit_path = self.dir / f"audit-{core['cpu']}-{raw}.jsonl"
        guard = self.session(core, transport=self.transport, lock_path=self.dir / f"l-{core['cpu']}-{raw}.lock")
        word = VfWord(core["favored"] & 0xFF, (core["fused"] >> 8) & 0xFFF, False, raw).to_u32()
        try:
            guard.execute(self.write_req(word, cpu=core["cpu"]), self.confirm)
            return True
        except GuardRefused as exc:
            self.assertIs(exc.reason, Refusal.CHECK_FAILED)
            return False

    def test_no_core_has_an_offset_floor(self) -> None:
        # D-049: the D-015 floors (P -50 mV with the assumed BIOS offset, E -100 mV) are gone: the 11-bit field is the only bound
        for core, raws in ((CPU1, (-51, -52, -300, -1024, 0)), (CPU8, (-102, -103, -500, -1024, 0))):
            for raw in raws:
                self.assertTrue(self.accepted(core, raw), (core["cpu"], raw))

    def test_a_positive_offset_needs_the_cpu_voltage_limit(self) -> None:
        self.assertFalse(self.accepted(CPU1, 1))  # cmd 0x28 is not part of this fixture's reads: the predicted voltage cannot be bounded

    def test_the_entry_minimum_still_applies_when_it_is_tighter(self) -> None:
        entries = tuple(dataclasses.replace(e, params={"offset_raw_min": -20}) if e.cmd == 0x11 else e for e in self.policy.allow)
        self.policy = dataclasses.replace(self.policy, allow=entries)
        self.assertTrue(self.accepted(CPU8, -20))
        self.assertFalse(self.accepted(CPU8, -21))

    def test_e_cpus_decide_the_kind_and_unknown_cpus_count_as_p(self) -> None:
        self.policy = dataclasses.replace(self.policy, e_cpus=frozenset({1}))  # CPU 1 listed as an E core
        self.assertEqual((self.policy.core_kind(1), self.policy.core_kind(8), self.policy.core_kind(500)), ("E", "P", "P"))

    def test_build_context_carries_the_bound_flag_the_cpu_limits_and_the_facts(self) -> None:
        policy = dataclasses.replace(self.policy, hw_ratio_max={"E": 99, "P": 120})
        c = build_context(policy, 8, favored_word=1, fused_word=0x4A02E, vf_word_sent=3, max_voltage_word=0x56D)  # top fused ratio = [7:0] of the live 0x09 word
        self.assertTrue(c.bound)
        self.assertEqual(
            (c.top_fused_ratio, c.favored_word, c.fused_word, c.vf_word_sent, c.max_voltage_word, c.hw_ratio_max, c.hw_v_max_raw),
            (46, 1, 0x4A02E, 3, 0x56D, 99, 0x56C),
        )
        self.assertEqual(build_context(policy, 1, favored_word=None, fused_word=None, vf_word_sent=None).hw_ratio_max, 120)  # P: its own kind's limit

    def test_the_unread_limits_are_none_and_the_context_is_still_bound(self) -> None:
        shipped = build_context(default_m1_policy(), 8, favored_word=1, fused_word=0x4A02E, vf_word_sent=3)
        self.assertTrue(shipped.bound)
        self.assertEqual((shipped.hw_ratio_max, shipped.hw_v_max_raw, shipped.top_fused_ratio), (None, None, 46))
        self.assertIsNone(build_context(default_m1_policy(), 8, favored_word=None, fused_word=None, vf_word_sent=None).top_fused_ratio)  # 0x09 not read: unknown, never a default


class FavoredCoreWrite(MeasuredCase):
    def after_vf(self, core=CPU1, word=0xFF653837):
        guard = self.session(core)
        self.vf_write(guard, core["cpu"], word)
        return guard

    def test_the_xtu_sequence_is_accepted_in_order(self) -> None:
        guard = self.after_vf()
        result = guard.execute(self.fav_write(1, 0x37), self.confirm)
        self.assertIs(result.verified, True)
        self.assertEqual(self.transport.calls[-2:], [(1, 0x1D, 0, 0, 0x37), (1, 0x1C, 0, 0, 0)])  # the read-back is the 0x1C twin
        self.assertIs(guard.execute(self.gran_write(1, 3), self.confirm).verified, True)
        self.assertFalse(guard.tripped)

    def test_data_must_equal_the_ratio_of_the_0x11_word_executed_for_the_cpu(self) -> None:
        guard = self.after_vf()
        for data in (0x36, 0x38, 0x39, 0x0, 0xFF, 0x3737):
            with self.subTest(data=hex(data)):
                self.assert_refused(guard, self.fav_write(1, data), Refusal.CHECK_FAILED)
        self.assertIs(guard.execute(self.fav_write(1, 0x37), self.confirm).verified, True)

    def test_no_0x11_for_the_cpu_means_no_0x1d(self) -> None:
        guard = self.session(CPU1)
        exc = self.assert_refused(guard, self.fav_write(1, 0x37), Refusal.CHECK_FAILED)
        self.assertIn("no 0x11 word was executed", exc.detail)
        guard = self.after_vf(CPU0, 0xFF65B439)  # a 0x11 on CPU 0 does not license a 0x1D on CPU 1
        self.read_all(guard, CPU1)
        self.assert_refused(guard, self.fav_write(1, 0x37), Refusal.CHECK_FAILED)

    def test_a_refused_or_failed_0x11_does_not_license_a_0x1d(self) -> None:
        guard = self.session(CPU1)
        self.assert_refused(guard, self.write_req(VfWord(0x37, 0x538, False, 80).to_u32(), cpu=1), Refusal.CHECK_FAILED)  # a positive offset with cmd 0x28 unread
        self.assert_refused(guard, self.fav_write(1, 0x37), Refusal.CHECK_FAILED)
        self.transport = RecordingTransport()
        guard = self.session(CPU1, transport=self.transport, lock_path=self.dir / "rc.lock")
        self.transport.rc_on_call[len(self.transport.calls) + 1] = 0x3  # pcode answers RatioExceedsLimits to the 0x11
        result = guard.execute(self.write_req(0xFF653837, cpu=1), self.confirm)
        self.assertEqual(result.response.rc, 3)
        self.assert_refused(guard, self.fav_write(1, 0x37), Refusal.CHECK_FAILED)

    def test_baseline_missing_for_0x1d_without_a_0x1c_read(self) -> None:
        # a policy that allows the 0x1D write without the 0x11 check being involved: the twin read is still demanded
        guard = self.make_guard()
        self.assert_refused(guard, self.fav_write(1, 0x37), Refusal.BASELINE_MISSING)

    def test_the_ratio_of_the_executed_word_is_followed_not_a_baseline_ceiling(self) -> None:
        # D-016: the 0x11 check owns the ceiling; the 0x1D check follows what was executed, so it keeps working when raises are allowed
        check = CHECKS["favored_ratio_follows_vf"]
        raised = VfWord(60, 0x538, False, -5).to_u32()  # a ratio above 0x1C[7:0] = 55 (refused by the default no-raise ceiling of the 0x11 check)
        req = SimpleNamespace(cpu=1, cmd=0x1D, p1=0, p2=0, data=60, purpose="t")
        self.assertEqual(check(req, 0x3737, {}, ctx(vf_word_sent=raised)), (True, "ok"))
        self.assertFalse(check(SimpleNamespace(data=59), 0x3737, {}, ctx(vf_word_sent=raised))[0])  # still has to be that ratio
        self.assertFalse(check(req, 0x3737, {}, None)[0])  # no context: refuse
        self.assertFalse(check(req, 0x3737, {}, ctx())[0])  # no executed 0x11
        zero = VfWord(0, 0x538, False, -5).to_u32()
        self.assertFalse(check(SimpleNamespace(data=0), 0x3737, {}, ctx(vf_word_sent=zero))[0])  # zero ratio, even if it "follows"

    def test_readback_compares_the_low_byte_only(self) -> None:
        guard = self.after_vf()
        self.transport.apply_write = lambda data: 0x2E00 | data  # 0x1C returns the fused ratio in [15:8] as well (set after the 0x11)
        result = guard.execute(self.fav_write(1, 0x37), self.confirm)
        self.assertEqual((result.verified, result.readback.data), (True, 0x2E37))
        self.assertFalse(guard.tripped)

    def test_a_wrong_low_byte_in_the_readback_trips_the_guard(self) -> None:
        guard = self.after_vf()
        self.transport.apply_write = lambda data: 0x3700 | (data ^ 1)  # (set after the 0x11 so only the 0x1D read-back is affected)
        result = guard.execute(self.fav_write(1, 0x37), self.confirm)
        self.assertIs(result.verified, False)
        self.assertTrue(guard.tripped)
        self.assertEqual(self.records()[-1]["event"], "trip")
        self.assert_refused(guard, self.read_req(), Refusal.TRIPPED)

    def test_a_readback_error_rc_trips_the_guard(self) -> None:
        guard = self.after_vf()
        self.transport.rc_on_call[len(self.transport.calls) + 2] = 0x13
        self.assertIs(guard.execute(self.fav_write(1, 0x37), self.confirm).verified, False)
        self.assertTrue(guard.tripped)


class GranularRatioWrite(MeasuredCase):
    def test_data_must_equal_the_0x26_word_read_for_the_cpu(self) -> None:
        guard = self.session(dict(CPU1, granular=0x205))
        for data in (0x3, 0x204, 0x206, 0x0, 0x205 | 0x10000):
            with self.subTest(data=hex(data)):
                self.assert_refused(guard, self.gran_write(1, data), Refusal.CHECK_FAILED)
        result = guard.execute(self.gran_write(1, 0x205), self.confirm)
        self.assertIs(result.verified, True)
        self.assertEqual(self.transport.calls[-2:], [(1, 0x27, 0, 0, 0x205), (1, 0x26, 0, 0, 0)])

    def test_a_0x27_without_a_0x26_read_for_the_cpu_is_refused_baseline_missing(self) -> None:
        guard = self.session(CPU1, ("10", "1C", "09"))
        self.assert_refused(guard, self.gran_write(1, 3), Refusal.BASELINE_MISSING)
        self.seed(CPU0)
        self.read_all(guard, CPU0, which=("26",))  # another CPU's 0x26 is not this CPU's
        self.assert_refused(guard, self.gran_write(1, 3), Refusal.BASELINE_MISSING)
        guard.execute(self.read_req(1, 0x26), self.confirm)
        self.assertIs(guard.execute(self.gran_write(1, 3), self.confirm).verified, True)

    def test_a_failed_0x26_read_is_no_baseline(self) -> None:
        self.seed(CPU1)
        guard = self.make_guard()
        self.transport.read_rc = 0x10
        guard.execute(self.read_req(1, 0x26), self.confirm)
        self.transport.read_rc = 0
        self.assert_refused(guard, self.gran_write(1, 3), Refusal.BASELINE_MISSING)

    def test_readback_is_exact(self) -> None:
        self.transport.apply_write = lambda data: data | 0x100  # a different word comes back
        guard = self.session(CPU1)
        result = guard.execute(self.gran_write(1, 3), self.confirm)
        self.assertIs(result.verified, False)
        self.assertEqual(result.readback.data, 0x103)
        self.assertTrue(guard.tripped)

    def test_the_check_function(self) -> None:
        check = CHECKS["granular_equals_read"]
        req = SimpleNamespace(cpu=1, cmd=0x27, p1=0, p2=0, data=3, purpose="t")
        self.assertEqual(check(req, 3, {}, None), (True, "ok"))  # needs no context: the baseline IS the 0x26 word
        self.assertFalse(check(req, 4, {}, None)[0])
        self.assertFalse(check(SimpleNamespace(data=0x103), 3, {}, None)[0])  # exact, not a mask


class RevertPlanIsXtuShaped(MeasuredCase):
    def apply_cpu(self, guard, core: dict, word: int) -> None:
        cpu = core["cpu"]
        self.vf_write(guard, cpu, word)
        guard.execute(self.fav_write(cpu, VfWord.from_u32(word).ratio), self.confirm)
        guard.execute(self.gran_write(cpu, core["granular"]), self.confirm)

    def test_a_zero_baseline_is_reverted_to_the_xtu_word_not_to_zero(self) -> None:
        guard = self.session(CPU1)
        self.apply_cpu(guard, CPU1, 0xFF653837)
        self.assertEqual(
            guard.revert_plan(),
            [Request(1, 0x11, 0, 0, 0x00000037, "revert"), Request(1, 0x1D, 0, 0, 0x37, "revert"), Request(1, 0x27, 0, 0, 3, "revert")],
        )  # S-04d: XTU's own revert for CPU 1 is 0x00053837 / 0x37 / 3; G-124: no OC point = V target 0, so the word is 0x00000037; the baseline 0x0 cannot be written back

    def test_cpu0_revert_keeps_its_ratio_override_and_fills_the_v_target(self) -> None:
        guard = self.session(CPU0)
        self.apply_cpu(guard, CPU0, 0xFE25B439)
        self.assertEqual([r.data for r in guard.revert_plan()], [0x00000039, 0x39, 3])  # S-04 + G-124: XTU's 0x0005B439 without the target it cannot keep

    def test_a_baseline_without_an_oc_point_is_reverted_to_the_word_the_hardware_keeps(self) -> None:
        """G-122 (D-035): baseline 0xFEC00039 = ratio 57 (no OC point), offset -10 mV, no target. Filling the target from 0x09 would write 0xFEC5B439, which the
        hardware drops (read-back trip); the revert is the baseline exactly. G-124: an offset-0 baseline gets no filled target either (0x39 -> 0x00000039)."""
        core = dict(CPU0, vf=0xFEC00039)
        guard = self.session(core)
        self.apply_cpu(guard, core, 0x0005B439)
        self.assertEqual([r.data for r in guard.revert_plan()], [0xFEC00039, 0x39, 3])
        guard = self.session(CPU0)
        self.apply_cpu(guard, CPU0, 0xFEC00039)
        self.assertEqual([r.data for r in guard.revert_plan()], [0x00000039, 0x39, 3])

    def test_an_e_baseline_of_zero_is_reverted_without_an_oc_point_unless_the_applied_word_had_one(self) -> None:
        """G-122: E 0x0 baseline (0x1C[7:0] = 48, top fused 46). A forward word at ratio 46 is undone at ratio 46 (a 48 would create an OC point); a forward word at
        48 (XTU's shape, an OC point) keeps the XTU fill 48."""
        e_core = dict(cpu=8, vf=0x0, favored=0x2E30, fused=0x4A02E, granular=0x3)
        guard = self.session(e_core)
        self.apply_cpu(guard, e_core, 0xFF60002E)
        self.assertEqual([r.data for r in guard.revert_plan()][:2], [0x0000002E, 0x2E])
        guard = self.session(e_core)
        self.apply_cpu(guard, e_core, 0xFF64A030)
        self.assertEqual([r.data for r in guard.revert_plan()][:2], [0x0004A030, 0x30])

    def test_the_revert_requests_pass_every_check_in_a_new_session(self) -> None:
        guard = self.session(CPU1)
        self.apply_cpu(guard, CPU1, 0xFF653837)
        plan = guard.revert_plan()
        self.transport = RecordingTransport()
        self.transport.state.update(
            {(1, 0x10, 0, 0): 0xFF653837, (1, 0x1C, 0, 0): 0x3737, (1, 0x09, 0x11, 0): 0x53839, (1, 0x26, 0, 0): 3}
        )
        self.audit_path = self.dir / "second.jsonl"
        second = self.make_guard(transport=self.transport, lock_path=self.dir / "second.lock")
        self.read_all(second, CPU1)
        for req in plan:
            self.assertIs(second.execute(req, self.confirm).verified, True, req)
        self.assertEqual(self.transport.state[(1, 0x10, 0, 0)], 0x00000037)

    def test_cpus_are_grouped_newest_first_with_their_0x11_before_their_0x1d_and_the_0x15_last(self) -> None:
        self.transport.state[(1, 0x14, 0, 0)] = 0x1
        self.seed(CPU1)
        self.seed(CPU8)
        guard = self.make_guard()
        guard.execute(self.read_req(1, 0x14), self.confirm)
        self.read_all(guard, CPU1)
        self.read_all(guard, CPU8)
        guard.execute(self.write_req(0x9, cpu=1, cmd=0x15), self.confirm)
        self.apply_cpu(guard, CPU1, 0xFF653837)
        self.apply_cpu(guard, CPU8, 0xFF64A030)
        plan = guard.revert_plan()
        self.assertEqual([(r.cpu, r.cmd) for r in plan], [(8, 0x11), (8, 0x1D), (8, 0x27), (1, 0x11), (1, 0x1D), (1, 0x27), (1, 0x15)])
        self.assertEqual([r.data for r in plan], [0x0004A030, 0x30, 3, 0x00000037, 0x37, 3, 0x1])

    def test_the_plan_is_pure(self) -> None:
        guard = self.session(CPU1)
        self.apply_cpu(guard, CPU1, 0xFF653837)
        sent = list(self.transport.calls)
        self.assertEqual(guard.revert_plan(), guard.revert_plan())
        self.assertEqual(self.transport.calls, sent)


class PolicyFields(unittest.TestCase):
    def test_defaults(self) -> None:
        p = default_m1_policy()
        self.assertEqual(p.e_cpus, frozenset(range(8, 24)))  # the unbound default: the simulated reference CPU's E CPUs
        self.assertEqual(p.core_kind(7), "P")
        self.assertEqual(p.core_kind(8), "E")
        self.assertEqual(dict(p.assumed_bios_offset_mv), {"P": 0, "E": 0})  # D-049: the removed D-015 fields read as neutral stand-ins
        self.assertEqual((p.effective_offset_floor_mv, p.offset_ceiling_mv), (-1000, 999))

    def test_the_removed_d015_fields_cannot_be_set(self) -> None:
        for kwargs in ({"assumed_bios_offset_mv": {"P": -50, "E": 0}}, {"effective_offset_floor_mv": -100}):
            with self.subTest(kwargs=kwargs), self.assertRaises(TypeError):
                Policy(cpus=frozenset({0}), **kwargs)  # type: ignore[arg-type]

    def test_invalid_values_are_rejected(self) -> None:
        for kwargs in ({"e_cpus": "8-23"}, {"e_cpus": [True]}, {"e_cpus": [-1]}):
            with self.subTest(kwargs=kwargs), self.assertRaises(PolicyError):
                Policy(cpus=frozenset({0}), **kwargs)

    def test_toml_keys(self) -> None:
        p = load_policy('cpus = [0, "8-9"]\ne_cpus = ["8-9"]\neffective_offset_floor_mv = -80\n[assumed_bios_offset_mv]\nP = -60\n')
        self.assertEqual(p.e_cpus, frozenset({8, 9}))
        self.assertEqual(p.ignored_keys, ("effective_offset_floor_mv", "assumed_bios_offset_mv"))  # D-049: loaded, ignored, named
        self.assertEqual((p.effective_offset_floor_mv, dict(p.assumed_bios_offset_mv)), (-1000, {"P": 0, "E": 0}))  # nothing of it is in force

    def test_bad_toml_values_are_load_errors(self) -> None:
        for text in ("cpus = [0]\ne_cpus = 8\n",):
            with self.subTest(text=text), self.assertRaises(PolicyError):
                load_policy(text)

    def test_vf_check_without_a_context_is_refused(self) -> None:
        req = SimpleNamespace(cpu=0, cmd=0x11, p1=0, p2=0, data=0x00053837, purpose="t")
        ok, detail = CHECKS["vf_bounded"](req, 0, {"offset_raw_min": -102})
        self.assertFalse(ok)
        self.assertIn("no check context", detail)
        self.assertFalse(CHECKS["vf_bounded"](req, 0, {"offset_raw_min": -102}, CheckContext())[0])  # not bound: the explicit flag replaced the old offset-floor sentinel


if __name__ == "__main__":
    unittest.main()
