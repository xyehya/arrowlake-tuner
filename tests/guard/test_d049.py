"""D-049 (operator, 2026-10-18): the helper enforces only what the CPU itself enforces, read at runtime, failing closed when unread.

Two halves, both at the predicate level (the guard's allowlist / read-back / order are the old tests'):

* every removed OPERATOR limit (offset window / ceiling, step limits, ratio_max, v_target_max, turbo shape) is now ALLOWED, and old policy files that still carry the keys LOAD, are ignored
  and warned about once;
* every CPU rule is refused AT THE CPU VALUE with its source cited: cmd 0x01 [7:0] (ratio), cmd 0x28 [11:0] - 1 LSB (voltage; field 0 = 2.0 V, one meaning for every reader), the 11-bit offset
  field, and the fail-closed ``CPU_LIMIT_UNREAD`` that names the read.
"""
from __future__ import annotations

import unittest

from arltuner.guard import Policy, PolicyError, Request, default_m1_policy, load_policy
from arltuner.guard.policy import (
    CPU_LIMIT_UNREAD,
    LEGACY_KEYS,
    MsrCheckContext,
    _check_turbo_ratio_bounded,
    _check_vf_bounded,
    _check_vf_point_offset_bounded,
    build_context,
    build_msr_context,
    hw_v_max_mv,
    hw_v_max_raw,
    ratio_limit,
    v_target_limit,
    v_target_limit_mv,
)
from arltuner.guard.guard import MsrRequest
from arltuner.mailbox import VfWord
from arltuner.topology import arl_s_285k

TOPOLOGY = arl_s_285k()
P_CPU, E_CPU = 1, 8
OC_120 = 0x778  # cmd 0x01 P1=0: 120x, ratio / voltage / offset OC ok [MEASURED win 2026-10-01]
VMAX_P, VMAX_E = 0x681, 0x56D  # cmd 0x28: 1625 mV / 1355 mV [MEASURED win 2026-10-01]
FAVORED_P = 0x3737  # 0x1C: 55x
FUSED_P = 0x53839  # 0x09: ratio 57, V target 0x538 = 1336 raw
P_V = 0x538


def word(ratio: int, v_raw: int, offset_raw: int = 0) -> int:
    return VfWord(ratio, v_raw, False, offset_raw).to_u32()


def policy() -> Policy:
    return default_m1_policy(TOPOLOGY)


def ctx(*, cpu: int = P_CPU, oc: tuple = (OC_120,), vmax: int | None = VMAX_P, p: Policy | None = None, favored: int = FAVORED_P, fused: int = FUSED_P):
    return build_context(p or policy(), cpu, favored_word=favored, fused_word=fused, vf_word_sent=None, max_voltage_word=vmax, oc_cap_words_of_kind=oc)


def vf(new: int, base: int = 0, c=None):
    req = Request(P_CPU, 0x11, 0, 0, new, "d049 test")
    return _check_vf_bounded(req, base, {}, c if c is not None else ctx())


class OneMeaningOfTheVoltageLimit(unittest.TestCase):
    """The old code had two: ``hw_v_max_raw(0)`` = 2048 (2.0 V, registers.md 0x28) but ``hw_v_max_mv(0)`` = None ("unread"). One meaning now, the registers.md one."""

    def test_field_0_is_2_0_v_for_every_reader(self) -> None:
        self.assertEqual(hw_v_max_raw(0), 2048)
        self.assertEqual(hw_v_max_mv(0), 2000)
        self.assertEqual(hw_v_max_raw(0x1000), 2048)  # only [11:0] counts
        p = policy()
        self.assertEqual(v_target_limit(p, "P", 0)[0], 2048)
        self.assertEqual(v_target_limit_mv(p, "P", 0)[0], 2000)

    def test_unread_is_none_and_nothing_else_is(self) -> None:
        self.assertIsNone(hw_v_max_mv(None))
        raw, text = v_target_limit(policy(), "P", None)
        self.assertIsNone(raw)
        self.assertTrue(text.startswith(CPU_LIMIT_UNREAD), text)
        self.assertIn("cmd 0x28", text)

    def test_measured_values(self) -> None:
        self.assertEqual((hw_v_max_mv(VMAX_P), hw_v_max_mv(VMAX_E)), (1625, 1355))
        self.assertEqual(hw_v_max_raw(VMAX_P), 0x680)  # XTU's raw - 1

    def test_the_check_honours_field_0_as_2_0_v(self) -> None:
        c = ctx(vmax=0)
        self.assertEqual(c.hw_v_max_raw, 2048)
        ok, _ = vf(word(57, 2048), word(57, P_V), c)
        self.assertTrue(ok)
        ok, detail = vf(word(57, 2049), word(57, P_V), c)
        self.assertFalse(ok)
        self.assertIn("cmd 0x28", detail)


class RemovedOperatorLimitsAreAllowed(unittest.TestCase):
    """Each of these was a refusal (policy offset window / ceiling, step limits, ratio_max, v_target_max). The CPU limits are bound (120x / 1625 mV), nothing else is."""

    def test_big_negative_offset_is_allowed(self) -> None:
        for mv in (-100, -250, -500, -1000):
            ok, detail = vf(word(55, 0, VfWord.mv_to_offset_raw(mv)), word(55, 0), ctx())
            self.assertTrue(ok, (mv, detail))

    def test_positive_offset_is_allowed_below_the_cpu_voltage_limit(self) -> None:
        ok, detail = vf(word(55, 0, VfWord.mv_to_offset_raw(60)), word(55, 0), ctx())  # 1336 + 61 raw = 1397 < 0x680 = 1664
        self.assertTrue(ok, detail)

    def test_ratio_step_of_ten_bins_is_allowed(self) -> None:
        ok, detail = vf(word(65, P_V), word(55, P_V), ctx())
        self.assertTrue(ok, detail)

    def test_v_target_step_of_150_mv_is_allowed(self) -> None:
        ok, detail = vf(word(57, P_V + 154), word(57, P_V), ctx())
        self.assertTrue(ok, detail)

    def test_ratio_above_what_a_policy_would_have_capped_is_allowed(self) -> None:
        ok, detail = vf(word(100, P_V), word(55, P_V), ctx())  # the old default / "auto" ceiling was fused + 3
        self.assertTrue(ok, detail)

    def test_point_offsets_have_no_window_and_no_step(self) -> None:
        base = VfWord(54, 0, False, 0).to_u32()
        for mv in (-500, 300):
            new = VfWord(54, 0, False, VfWord.mv_to_offset_raw(mv)).to_u32()
            ok, detail = _check_vf_point_offset_bounded(Request(P_CPU, 0x11, 0, 5, new, "d049"), base, {}, ctx())
            self.assertTrue(ok, (mv, detail))

    def test_the_allow_entry_params_still_tighten_a_point(self) -> None:
        base = VfWord(54, 0, False, 0).to_u32()
        new = VfWord(54, 0, False, -300).to_u32()
        ok, detail = _check_vf_point_offset_bounded(Request(P_CPU, 0x11, 0, 5, new, "d049"), base, {"offset_raw_min": -100}, ctx())
        self.assertFalse(ok)
        self.assertIn("-100", detail)

    def test_turbo_table_shape_is_not_a_refusal(self) -> None:
        old = _table([57, 57, 56, 55, 54, 54, 53, 52])
        for label, new in (
            ("rising with the active-core count", _table([52, 53, 54, 54, 55, 56, 57, 57])),
            ("a 10-bin step", _table([67, 67, 66, 65, 64, 64, 63, 62])),
            ("below the base ratio", _table([30, 30, 30, 30, 30, 30, 30, 30])),
        ):
            ok, detail = _turbo(new, old, MsrCheckContext("P", 120, "CPU limit 120x"))
            self.assertTrue(ok, (label, detail))


class OldPolicyFilesStillLoad(unittest.TestCase):
    OLD = (
        'cpus = "auto"\nmax_ratio_step = 8\nmax_v_step_mv = 200\nmax_offset_step_mv = 200\noffset_ceiling_mv = 200\neffective_offset_floor_mv = -200\n'
        'assumed_bios_offset_mv = { P = 0, E = 0 }\n[ratio_max]\nP = "auto"\nE = 49\n[v_target_max_mv]\nP = "auto"\n[power]\npl1_w = [125, 253]\nmax_step_w = 50\n[runtime]\ncap_ratio_min = 8\n'
    )

    def test_loads_ignores_and_names_the_keys_once(self) -> None:
        with self.assertLogs("arltuner.policy", level="WARNING") as logs:
            p = load_policy(self.OLD, TOPOLOGY)
        self.assertEqual(len(logs.records), 1)
        line = logs.records[0].getMessage()
        for key in ("max_ratio_step", "offset_ceiling_mv", "ratio_max", "v_target_max_mv", "power.pl1_w", "power.max_step_w", "runtime.cap_ratio_min", "assumed_bios_offset_mv"):
            self.assertIn(key, line)
            self.assertIn(key, p.ignored_keys)
        self.assertEqual(set(LEGACY_KEYS), {"effective_offset_floor_mv", "assumed_bios_offset_mv", "offset_ceiling_mv", "max_ratio_step", "max_v_step_mv", "max_offset_step_mv", "ratio_max", "v_target_max_mv", "power", "runtime"})

    def test_the_ignored_values_do_not_even_have_to_be_valid(self) -> None:
        p = load_policy('cpus = "auto"\nmax_ratio_step = "x"\n[ratio_max]\nQ = -5\n', TOPOLOGY)
        self.assertEqual(set(p.ignored_keys), {"max_ratio_step", "ratio_max"})

    def test_a_policy_without_them_does_not_warn(self) -> None:
        with self.assertNoLogs("arltuner.policy", level="WARNING"):
            p = load_policy('cpus = "auto"\n', TOPOLOGY)
        self.assertEqual(p.ignored_keys, ())

    def test_unknown_keys_are_still_errors(self) -> None:
        with self.assertRaises(PolicyError):
            load_policy('cpus = "auto"\nmystery_limit = 1\n', TOPOLOGY)

    def test_the_old_attributes_read_as_no_limit(self) -> None:
        p = load_policy(self.OLD, TOPOLOGY)
        self.assertEqual(dict(p.ratio_max), {"P": None, "E": None})
        self.assertEqual((p.offset_ceiling_mv, p.effective_offset_floor_mv), (999, -1000))
        self.assertIsNone(p.power)
        self.assertIsNone(p.runtime)


class CpuRatioLimit(unittest.TestCase):
    """cmd 0x01 [7:0] (registers.md): refused at the CPU value, source cited; unread = a raise is refused naming the read."""

    def test_above_the_cpu_limit_is_refused_with_the_register(self) -> None:
        c = ctx(oc=(0x100 | 90,))  # 90x, ratio OC ok
        ok, detail = vf(word(91, P_V), word(55, P_V), c)
        self.assertFalse(ok)
        self.assertIn("91", detail)
        self.assertIn("90x", detail)
        self.assertIn("cmd 0x01", detail)
        ok, detail = vf(word(90, P_V), word(55, P_V), c)
        self.assertTrue(ok, detail)  # AT the limit

    def test_the_lowest_word_of_the_kind_wins(self) -> None:
        cap, _ = ratio_limit(policy(), "P", [0x100 | 90, 0x100 | 84])
        self.assertEqual(cap, 84)

    def test_unread_refuses_a_raise_and_names_the_read(self) -> None:
        c = ctx(oc=())
        ok, detail = vf(word(60, P_V), word(55, P_V), c)  # above the 57 fused and the 55 favored
        self.assertFalse(ok)
        self.assertIn(CPU_LIMIT_UNREAD, detail)
        self.assertIn("cmd 0x01", detail)

    def test_unread_still_allows_no_raise(self) -> None:
        c = ctx(oc=())
        ok, detail = vf(word(55, P_V), word(55, P_V), c)
        self.assertTrue(ok, detail)
        ok, detail = vf(word(50, P_V), word(55, P_V), c)  # lowering
        self.assertTrue(ok, detail)

    def test_read_as_unsupported_is_a_different_message(self) -> None:
        c = ctx(oc=(0x078,))  # bit 8 clear: ratio overclocking not supported
        ok, detail = vf(word(60, P_V), word(55, P_V), c)
        self.assertFalse(ok)
        self.assertIn("not supported", detail)
        self.assertNotIn(CPU_LIMIT_UNREAD, detail)

    def test_the_binding_from_tune_read_stands_in_for_the_session_word(self) -> None:
        p = policy().with_hw_limits({"P": 90, "E": 90}, {"P": 1625, "E": 1355})
        c = ctx(oc=(), p=p)
        self.assertEqual(c.hw_ratio_max, 90)
        ok, detail = vf(word(91, P_V), word(55, P_V), c)
        self.assertFalse(ok)
        self.assertIn("90x", detail)


class CpuVoltageLimit(unittest.TestCase):
    """cmd 0x28 [11:0] - 1 LSB (XTU's raw - 1 rule): the predicted voltage (V target or fused voltage, plus a positive offset) may not exceed it."""

    def test_a_v_target_above_the_limit_is_refused_at_the_limit(self) -> None:
        limit = hw_v_max_raw(VMAX_P)
        ok, detail = vf(word(57, limit), word(57, P_V))
        self.assertTrue(ok, detail)  # at the limit
        ok, detail = vf(word(57, limit + 1), word(57, P_V))
        self.assertFalse(ok)
        self.assertIn("cmd 0x28", detail)
        self.assertIn("1625 mV", detail)

    def test_a_positive_offset_counts_toward_the_prediction(self) -> None:
        limit = hw_v_max_raw(VMAX_P)
        over = limit - P_V + 1  # P_V + over = limit + 1
        ok, detail = vf(word(55, 0, over), word(55, 0))  # no OC point: the fused voltage is the reference
        self.assertFalse(ok)
        self.assertIn("predicted voltage", detail)
        ok, detail = vf(word(55, 0, over - 1), word(55, 0))
        self.assertTrue(ok, detail)

    def test_negative_offsets_never_need_the_limit(self) -> None:
        c = ctx(vmax=None)
        ok, detail = vf(word(55, 0, -500), word(55, 0), c)
        self.assertTrue(ok, detail)

    def test_unread_refuses_a_voltage_raise_and_names_the_read(self) -> None:
        c = ctx(vmax=None)
        for new in (word(57, P_V + 5), word(55, 0, 40)):
            ok, detail = vf(new, word(55, 0) if new == word(55, 0, 40) else word(57, P_V), c)
            self.assertFalse(ok, hex(new))
            self.assertIn(CPU_LIMIT_UNREAD, detail)
            self.assertIn("cmd 0x28", detail)

    def test_unread_still_allows_a_lowering(self) -> None:
        ok, detail = vf(word(57, P_V - 40), word(57, P_V), ctx(vmax=None))
        self.assertTrue(ok, detail)

    def test_each_domain_uses_its_own_limit(self) -> None:
        p = policy().with_hw_limits({"P": 120, "E": 120}, {"P": 1625, "E": 1355})
        self.assertEqual(ctx(cpu=P_CPU, vmax=None, p=p).hw_v_max_raw, 1625 * 1024 // 1000)
        self.assertEqual(ctx(cpu=E_CPU, vmax=None, p=p).hw_v_max_raw, 1355 * 1024 // 1000)


class TargetClampedUpToTheFusedPoint(unittest.TestCase):
    """cpu-rules-audit A:38: the CPU clamps a V target below the top fused point UP to it, so the predicted voltage is max(target, fused) + a positive offset."""

    FUSED_1300 = 0x53800 | 57 | ((1331 - 0x538) << 8)  # fused V 1331 raw ~ 1.3 V

    def test_a_low_target_with_a_big_positive_offset_is_judged_from_the_fused_voltage(self) -> None:
        low = round(1.0 * 1024)  # target 1.0 V
        plus_600 = VfWord.mv_to_offset_raw(600)  # 614 raw: 1.0 V + 600 mV = 1.6 V < 1.625 V, but the CPU clamps the target up to 1.3 V -> ~1.9 V
        c = ctx(fused=0x39 | (1331 << 8))
        ok, detail = vf(word(57, low, plus_600), word(57, low), c)
        self.assertFalse(ok)
        self.assertIn("predicted voltage", detail)
        self.assertIn("cmd 0x28", detail)
        ok, detail = vf(word(57, low, 0), word(57, low), c)  # no offset: the fused voltage 1331 < 0x680
        self.assertTrue(ok, detail)

    def test_a_target_below_the_fused_voltage_alone_predicts_the_fused_voltage(self) -> None:
        p = policy().with_hw_limits(None, None, {"P": 1300})  # a limit BELOW the fused voltage
        c = ctx(vmax=None, p=p, fused=0x39 | (1331 << 8))
        ok, detail = vf(word(57, 1024), word(57, 1331), c)
        self.assertFalse(ok)
        self.assertIn("fused 0x533", detail)

    def test_a_positive_offset_with_the_fused_voltage_unread_is_cpu_limit_unread(self) -> None:
        c = build_context(policy(), P_CPU, favored_word=FAVORED_P, fused_word=None, vf_word_sent=None, max_voltage_word=VMAX_P, oc_cap_words_of_kind=(OC_120,))
        ok, detail = vf(word(57, P_V, 10), word(57, P_V), c)
        self.assertFalse(ok)
        self.assertIn(CPU_LIMIT_UNREAD, detail)
        self.assertIn("0x09", detail)


class PositivePointOffsetsAgainstTheDomainLimit(unittest.TestCase):
    """Per-point (core P2 1..15, ring P1=2) positive offsets: base + offset <= the 0x28 limit of the domain; unread base or limit = CPU_LIMIT_UNREAD; negative offsets need no ceiling."""

    LIMIT = hw_v_max_raw(VMAX_P)  # 0x680

    def point(self, offset_raw: int, *, base_v: int = 0, c=None, p1: int = 0, p2: int = 5, base_offset: int = 0):
        base = VfWord(54, base_v, False, base_offset).to_u32()
        new = VfWord(54, base_v, False, offset_raw).to_u32()
        return _check_vf_point_offset_bounded(Request(P_CPU, 0x11, p1, p2, new, "d049"), base, {}, c if c is not None else ctx())

    def test_core_point_at_the_limit_and_one_over(self) -> None:
        at = self.LIMIT - P_V  # fused top voltage + at == the limit
        ok, detail = self.point(at)
        self.assertTrue(ok, detail)
        ok, detail = self.point(at + 1)
        self.assertFalse(ok)
        self.assertIn("predicted voltage", detail)
        self.assertIn("cmd 0x28", detail)

    def test_the_points_own_v_target_is_the_base_when_it_has_one(self) -> None:
        ok, detail = self.point(10, base_v=self.LIMIT - 10)
        self.assertTrue(ok, detail)
        ok, detail = self.point(11, base_v=self.LIMIT - 10)
        self.assertFalse(ok)

    def test_limit_unread_refuses_a_positive_offset_and_names_the_read(self) -> None:
        ok, detail = self.point(10, c=ctx(vmax=None))
        self.assertFalse(ok)
        self.assertIn(CPU_LIMIT_UNREAD, detail)
        self.assertIn("cmd 0x28", detail)

    def test_base_unread_refuses_a_positive_offset(self) -> None:
        c = build_context(policy(), P_CPU, favored_word=FAVORED_P, fused_word=None, vf_word_sent=None, max_voltage_word=VMAX_P, oc_cap_words_of_kind=(OC_120,))
        ok, detail = self.point(10, c=c)
        self.assertFalse(ok)
        self.assertIn(CPU_LIMIT_UNREAD, detail)

    def test_negative_offsets_and_lowerings_need_no_ceiling(self) -> None:
        bare = build_context(policy(), P_CPU, favored_word=None, fused_word=None, vf_word_sent=None)
        for raw in (-1024, -1, 0):
            ok, detail = self.point(raw, c=bare)
            self.assertTrue(ok, (raw, detail))
        ok, detail = self.point(5, c=bare, base_offset=20)  # lowers a positive offset
        self.assertTrue(ok, detail)

    def test_ring_uses_the_ring_limit_of_p1_2(self) -> None:
        ring_vmax = 0x600  # 1536 mV - 1 LSB = 0x5FF
        c = build_context(policy(), P_CPU, favored_word=FAVORED_P, fused_word=FUSED_P, vf_word_sent=None, max_voltage_word=VMAX_P, ring_max_voltage_word=ring_vmax)
        self.assertEqual(c.hw_ring_v_max_raw, 0x5FF)
        base_v = 0x5F0
        ok, detail = self.point(0xF, base_v=base_v, c=c, p1=2, p2=3)  # 0x5F0 + 0xF = 0x5FF: at the ring limit (the core limit 0x680 would allow more)
        self.assertTrue(ok, detail)
        ok, detail = self.point(0x10, base_v=base_v, c=c, p1=2, p2=3)
        self.assertFalse(ok)
        self.assertIn("ring", detail)

    def test_ring_unread_limit_or_base_is_cpu_limit_unread(self) -> None:
        ring_vmax = 0x600
        with_limit = build_context(policy(), P_CPU, favored_word=FAVORED_P, fused_word=FUSED_P, vf_word_sent=None, max_voltage_word=VMAX_P, ring_max_voltage_word=ring_vmax)
        ok, detail = self.point(5, c=with_limit, p1=2, p2=3)  # the point word has no V target and a ring has no readable fused voltage
        self.assertFalse(ok)
        self.assertIn(CPU_LIMIT_UNREAD, detail)
        ok, detail = self.point(5, base_v=0x500, c=ctx(), p1=2, p2=3)  # ring limit unread (the core's 0x28 is not the ring's)
        self.assertFalse(ok)
        self.assertIn(CPU_LIMIT_UNREAD, detail)
        self.assertIn("ring", detail)

    def test_the_bound_ring_limit_stands_in_for_the_session_word(self) -> None:
        p = policy().with_hw_limits(None, None, {"ring": 0x5FF})
        c = ctx(p=p)
        self.assertEqual(c.hw_ring_v_max_raw, 0x5FF)
        ok, detail = self.point(0xF, base_v=0x5F0, c=c, p1=2, p2=3)
        self.assertTrue(ok, detail)


class TheLimitIsStoredRaw(unittest.TestCase):
    def test_e_0x56c_stays_exact(self) -> None:
        raw = hw_v_max_raw(0x56D)  # 0x56C = 1388 raw = 1355.47 mV; whole mV (1355) rounded back to raw is 1387
        self.assertEqual(raw, 1388)
        p = policy().with_hw_limits(None, {"E": hw_v_max_mv(0x56D)}, {"E": raw})
        self.assertEqual(v_target_limit(p, "E", None)[0], 1388)
        p_mv_only = policy().with_hw_limits(None, {"E": 1355})
        self.assertEqual(v_target_limit(p_mv_only, "E", None)[0], 1387)  # the old whole-mV binding: one LSB on the safe side


class OffsetField(unittest.TestCase):
    def test_the_field_extremes_are_accepted_by_the_check(self) -> None:
        for raw in (-1024, 1023):
            VfWord(55, 0, False, raw)  # the codec
        self.assertEqual(VfWord.mv_to_offset_raw(-1000), -1024)
        self.assertEqual(VfWord.mv_to_offset_raw(999), 1023)
        ok, detail = vf(word(55, 0, -1024), word(55, 0))
        self.assertTrue(ok, detail)

    def test_beyond_the_field_cannot_be_encoded(self) -> None:
        for mv in (-1001, 1000):
            with self.assertRaises(ValueError):
                VfWord.mv_to_offset_raw(mv)


class NoContextIsRefused(unittest.TestCase):
    def test_none_and_an_unbound_context_are_refused(self) -> None:
        from arltuner.guard.policy import CheckContext

        req = Request(P_CPU, 0x11, 0, 0, word(55, P_V), "d049")
        for c in (None, CheckContext()):
            ok, detail = _check_vf_bounded(req, word(55, P_V), {}, c)
            self.assertFalse(ok)
            self.assertIn("no check context", detail)
        point = Request(P_CPU, 0x11, 0, 5, VfWord(54, 0, False, -10).to_u32(), "d049")
        for c in (None, CheckContext()):
            ok, detail = _check_vf_point_offset_bounded(point, VfWord(54, 0, False, 0).to_u32(), {}, c)
            self.assertFalse(ok)

    def test_build_context_binds(self) -> None:
        self.assertTrue(ctx().bound)


def _table(buckets: list[int]) -> int:
    return sum(b << (8 * k) for k, b in enumerate(buckets))


def _turbo(new: int, old: int, c: MsrCheckContext | None):
    return _check_turbo_ratio_bounded(MsrRequest(0, 0x1AD, new, "d049", "write", 8), old, {}, c)


class TurboTableCpuLimit(unittest.TestCase):
    OLD = _table([57, 57, 56, 55, 54, 54, 53, 52])

    def test_above_the_cpu_limit_is_refused(self) -> None:
        c = MsrCheckContext("P", 58, "CPU limit 58x (cmd 0x01 [7:0] fused max OC ratio of a P CPU, registers.md)")
        ok, detail = _turbo(_table([59, 58, 58, 58, 58, 58, 58, 58]), self.OLD, c)
        self.assertFalse(ok)
        self.assertIn("59", detail)
        self.assertIn("cmd 0x01", detail)
        ok, detail = _turbo(_table([58, 58, 58, 58, 58, 58, 58, 58]), self.OLD, c)
        self.assertTrue(ok, detail)

    def test_a_raise_with_the_limit_unread_is_refused_and_names_the_read(self) -> None:
        c = build_msr_context(policy(), 0x1AD)
        self.assertIsNone(c.hw_ratio_max)
        ok, detail = _turbo(_table([58, 57, 56, 55, 54, 54, 53, 52]), self.OLD, c)
        self.assertFalse(ok)
        self.assertIn(CPU_LIMIT_UNREAD, detail)
        self.assertIn("cmd 0x01", detail)

    def test_a_lowering_never_needs_the_limit(self) -> None:
        c = build_msr_context(policy(), 0x1AD)
        ok, detail = _turbo(_table([50, 50, 50, 50, 50, 50, 50, 50]), self.OLD, c)
        self.assertTrue(ok, detail)

    def test_a_zero_bucket_is_still_refused(self) -> None:
        ok, detail = _turbo(_table([57, 57, 56, 55, 54, 54, 53, 0]), self.OLD, MsrCheckContext("P", 120, ""))
        self.assertFalse(ok)
        self.assertIn("bucket 7", detail)

    def test_the_bound_policy_limit_is_used(self) -> None:
        p = policy().with_hw_limits({"P": 58})
        self.assertEqual(build_msr_context(p, 0x1AD).hw_ratio_max, 58)
        self.assertIsNone(build_msr_context(p, 0x650).hw_ratio_max)  # E is unread


if __name__ == "__main__":
    unittest.main()
