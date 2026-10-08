"""T-021 tests for the measured baseline (``msr/measured_baseline.py``), ``SimConfig.measured()`` and the new simulator semantics
(0x1C/0x1D favored word, 0x26/0x27 granular word, per-CPU 0x09 fused point and 0x28, ``reject_zero_fields``, ``fixed_reads``).

The reference values are transcribed from docs/research/win-xtu-live-trace.md S-03d (LP-indexed, Windows numbering) and S-04...S-07;
the module under test stores them in Linux CPU numbering, so the cross-check goes through the order-preserving LP map.
Everything here is simulator-only; the simulator's rule behaviour (which rc for which violation) is SIM-ONLY, not a hardware fact.
"""
from __future__ import annotations

import dataclasses
import unittest

from arltuner.mailbox import Cmd, Domain, Rc, Response, VfWord
from arltuner.msr import MailboxTransport, SimConfig, SimMsrPort, measured_baseline as m

from ._support import FakeClock

# S-03d, XTU level-4 log, indexed by Windows logical processor (LP)
LP_FUSED = {0: 0x5B439, 1: 0x53839, 10: 0x57439, 11: 0x59439, 12: 0x54439, 13: 0x56C39, 22: 0x56039, 23: 0x58039}
LP_FUSED.update({lp: 0x4A02E for lp in (2, 3, 4, 5)})
LP_FUSED.update({lp: 0x48C2E for lp in (*range(6, 10), *range(14, 22))})
LP_FAVORED = {lp: (0x3939 if lp in (0, 11) else 0x3737) for lp in (0, 1, 10, 11, 12, 13, 22, 23)}
LP_FAVORED.update({lp: 0x2E30 for lp in (*range(2, 10), *range(14, 22))})
LP_VF = {lp: (0x39 if lp == 0 else 0x0) for lp in range(24)}

RD_VF, WR_VF = int(Cmd.VOLTAGE_FREQ_READ), int(Cmd.VOLTAGE_FREQ_WRITE)
RD_FAV, WR_FAV = int(Cmd.FAVORED_CORE_READ), int(Cmd.FAVORED_CORE_WRITE)
RD_GRAN, WR_GRAN = int(Cmd.GRANULAR_RATIO_READ), int(Cmd.GRANULAR_RATIO_WRITE)
RD_FUSED = int(Cmd.VOLTAGE_FLOOR_READ)
RD_MAXV = int(Cmd.MAXIMUM_VOLTAGE_LIMIT_READ)


class Rig(unittest.TestCase):
    def use(self, **overrides) -> SimMsrPort:
        self.port = SimMsrPort(SimConfig.measured(**overrides))
        self.fc = FakeClock()
        return self.port

    def setUp(self) -> None:
        self.use()

    def tx(self, cpu: int, cmd: int, p1: int = 0, p2: int = 0, data: int = 0) -> Response:
        return MailboxTransport(self.port, sleep=self.fc.sleep, clock=self.fc.clock, timeout_s=1.0).transact(cpu, cmd, p1, p2, data)


class TablesAreTheMeasuredOnes(unittest.TestCase):
    def test_linux_tables_equal_the_lp_indexed_log_through_the_lp_map(self) -> None:
        self.assertEqual(sorted(m.LP_OF_CPU), list(range(24)))
        self.assertEqual(sorted(m.LP_OF_CPU.values()), list(range(24)))  # a permutation: every LP exactly once
        for cpu in range(24):
            lp = m.LP_OF_CPU[cpu]
            self.assertEqual(m.FUSED_POINT[cpu], LP_FUSED[lp], (cpu, lp))
            self.assertEqual(m.FAVORED_WORD[cpu], LP_FAVORED[lp], (cpu, lp))
            self.assertEqual(m.VF_DOMAIN0[cpu], LP_VF[lp], (cpu, lp))

    def test_the_lp_map_is_the_order_preserving_one(self) -> None:
        self.assertEqual([m.LP_OF_CPU[c] for c in range(8)], [0, 1, 10, 11, 12, 13, 22, 23])
        self.assertEqual([m.LP_OF_CPU[c] for c in range(8, 12)], [2, 3, 4, 5])
        self.assertEqual([m.LP_OF_CPU[c] for c in range(12, 16)], [6, 7, 8, 9])
        self.assertEqual([m.LP_OF_CPU[c] for c in range(16, 20)], [14, 15, 16, 17])
        self.assertEqual([m.LP_OF_CPU[c] for c in range(20, 24)], [18, 19, 20, 21])

    def test_the_task_values_in_linux_numbering(self) -> None:
        self.assertEqual({c: w for c, w in m.VF_DOMAIN0.items() if w}, {0: 0x39})  # only CPU 0 has a word; 23 CPUs are 0x0
        self.assertEqual(m.MISC_GLOBAL_CONFIG, 0x8)
        self.assertEqual(set(m.GRANULAR_WORD.values()), {3})
        self.assertEqual([m.MAX_VOLTAGE_RAW[c] for c in (0, 7, 8, 23)], [0x681, 0x681, 0x56D, 0x56D])
        self.assertEqual([m.FUSED_POINT[c] for c in range(8)], [0x5B439, 0x53839, 0x57439, 0x59439, 0x54439, 0x56C39, 0x56039, 0x58039])
        self.assertEqual({m.FUSED_POINT[c] for c in range(8, 12)}, {0x4A02E})
        self.assertEqual({m.FUSED_POINT[c] for c in range(12, 24)}, {0x48C2E})
        self.assertEqual([m.FAVORED_WORD[c] for c in range(8)], [0x3939, 0x3737, 0x3737, 0x3939, 0x3737, 0x3737, 0x3737, 0x3737])
        self.assertEqual({m.FAVORED_WORD[c] for c in range(8, 24)}, {0x2E30})
        self.assertEqual(dict(m.DOMAIN_VF), {1: 0xF8600000, 2: 0x28, 7: 0x20})

    def test_the_tables_are_read_only_and_tagged(self) -> None:
        for table in (m.VF_DOMAIN0, m.FUSED_POINT, m.FAVORED_WORD, m.GRANULAR_WORD, m.MAX_VOLTAGE_RAW, m.DOMAIN_VF, m.FIXED_READS, m.LP_OF_CPU):
            with self.assertRaises(TypeError):
                table[0] = 1  # type: ignore[index]
        self.assertEqual(m.TAG, "[MEASURED win 2026-10-01]")

    def test_the_gt_word_is_minus_59_6_mv(self) -> None:
        self.assertAlmostEqual(VfWord.from_u32(m.DOMAIN_VF[1]).offset_mv, -59.57, places=2)


class MeasuredMachine(Rig):
    def test_per_core_vf_words(self) -> None:
        for cpu in range(24):
            self.assertEqual(self.tx(cpu, RD_VF), Response(Rc.Pass, 0x39 if cpu == 0 else 0x0), cpu)
        self.assertEqual(self.port.vf(1), VfWord(0, 0, False, 0))  # a zero word, not a made-up default

    def test_other_per_core_reads(self) -> None:
        for cpu in range(24):
            self.assertEqual(self.tx(cpu, RD_FUSED, 0x11, 0), Response(Rc.Pass, m.FUSED_POINT[cpu]), cpu)
            self.assertEqual(self.tx(cpu, RD_FAV), Response(Rc.Pass, m.FAVORED_WORD[cpu]), cpu)
            self.assertEqual(self.tx(cpu, RD_GRAN), Response(Rc.Pass, 3), cpu)
            self.assertEqual(self.tx(cpu, RD_MAXV, 0, 0), Response(Rc.Pass, 0x681 if cpu < 8 else 0x56D), cpu)
        self.assertEqual(self.tx(5, int(Cmd.MISC_GLOBAL_CONFIG_READ)), Response(Rc.Pass, 0x8))
        self.assertTrue(self.port.per_core_enabled)

    def test_package_wide_words(self) -> None:
        cases = {
            (RD_VF, 1): 0xF8600000, (RD_VF, 2): 0x28, (RD_VF, 7): 0x20,
            (int(Cmd.OC_CAPABILITY_READ), 0): 0x778, (int(Cmd.OC_CAPABILITY_READ), 1): 0x778,
            (int(Cmd.OC_CAPABILITY_READ), 2): 0x755, (int(Cmd.OC_CAPABILITY_READ), 7): 0x755, (int(Cmd.OC_CAPABILITY_READ), 4): 0x600,
            (int(Cmd.PER_CORE_CAPABILITY_READ), 0): 0x36363939, (int(Cmd.PER_CORE_CAPABILITY_READ), 1): 0x36363636,
            (int(Cmd.ICC_MAX_READ), 0): 0x640, (int(Cmd.ICC_MAX_READ), 1): 0xC8,
            (int(Cmd.ICC_MAX_VR_READ), 0): 0x84002000,
            (int(Cmd.RESIDENCY_STATE_REGULATION_READ), 0): 0x1E,
            (int(Cmd.AVX_OFFSET_CONTROL_READ), 0): 0x0,
        }
        for (cmd, p1), word in cases.items():
            with self.subTest(cmd=hex(cmd), p1=p1):
                self.assertEqual(self.tx(3, cmd, p1, 0), Response(Rc.Pass, word))

    def test_capability_p1_3_is_still_rc_5_and_unmeasured_reads_are_not_invented(self) -> None:
        self.assertEqual(self.tx(0, int(Cmd.OC_CAPABILITY_READ), 3, 0), Response(Rc.OCNotSupported, 0))
        self.assertEqual(self.tx(0, int(Cmd.REFERENCE_CLK_MONITOR_READ), 0, 0), Response(Rc.UnrecognizedCommand, 0))  # only a range was measured
        self.assertEqual(self.tx(0, int(Cmd.ICC_MAX_READ), 5, 0), Response(Rc.UnrecognizedCommand, 0))  # only P1 0 and 1

    def test_xtus_cpu0_sequence_is_accepted_and_reads_back(self) -> None:
        self.use(reject_zero_fields=True)
        self.assertEqual(self.tx(0, WR_VF, 0, 0, 0xFF65B439), Response(Rc.Pass, 0xFF65B439))
        self.assertEqual(self.tx(0, WR_FAV, 0, 0, 0x39), Response(Rc.Pass, 0x39))
        self.assertEqual(self.tx(0, RD_GRAN), Response(Rc.Pass, 3))
        self.assertEqual(self.tx(0, WR_GRAN, 0, 0, 3), Response(Rc.Pass, 3))
        self.assertEqual(self.tx(0, RD_VF), Response(Rc.Pass, 0xFF65B439))  # XTU's read-back
        self.assertEqual(self.tx(0, WR_VF, 0, 0, 0x0005B439), Response(Rc.Pass, 0x0005B439))  # XTU's revert word: not the original 0x39
        self.assertEqual(self.tx(0, RD_VF).data, 0x0005B439)
        self.assertEqual(self.tx(1, RD_VF).data, 0x0)  # CPU 1 untouched

    def test_xtus_e_core_words_are_accepted(self) -> None:
        self.use(reject_zero_fields=True)
        self.assertEqual(self.tx(8, WR_VF, 0, 0, 0xFF64A030).rc, Rc.Pass)
        self.assertEqual(self.tx(12, WR_VF, 0, 0, 0xFF648C30).rc, Rc.Pass)
        for cpu in (8, 12):
            self.assertEqual(self.tx(cpu, WR_FAV, 0, 0, 0x30).rc, Rc.Pass)
            self.assertEqual(self.tx(cpu, WR_GRAN, 0, 0, 3).rc, Rc.Pass)

    def test_cpu_numbering_is_linux_not_lp(self) -> None:
        self.assertEqual(self.tx(4, RD_FUSED, 0x11, 0).data, 0x54439)  # P4 (Linux 4) = LP 12 = 1.316 V
        self.assertEqual(self.tx(11, RD_FUSED, 0x11, 0).data, 0x4A02E)  # the 1.156 V cluster ends at Linux CPU 11
        self.assertEqual(self.tx(12, RD_FUSED, 0x11, 0).data, 0x48C2E)


class ZeroFieldKnob(Rig):
    def test_default_is_off_and_a_zero_field_word_is_accepted(self) -> None:
        self.assertFalse(SimConfig().reject_zero_fields)
        self.assertFalse(SimConfig.measured().reject_zero_fields)
        self.assertEqual(self.tx(1, WR_VF, 0, 0, 0xFF000038).rc, Rc.Pass)  # V target 0, offset -5
        self.assertEqual(self.tx(1, WR_VF, 0, 0, 0xFF053800).rc, Rc.Pass)  # ratio 0

    def test_on_it_rejects_ratio_zero_and_v_target_zero_in_domain_0_only(self) -> None:
        self.use(reject_zero_fields=True)
        for word in (0xFF000038, 0xFF053800, 0x0, 0x00000039, 0x00053800):  # V target 0 / ratio 0 / both / V target 0 / ratio 0
            with self.subTest(word=hex(word)):
                self.assertEqual(self.tx(1, WR_VF, 0, 0, word), Response(Rc.InvalidParam, 0))
                self.assertEqual(self.tx(1, RD_VF).data, 0)  # nothing was stored
        self.assertEqual(self.tx(1, WR_VF, 0, 0, 0x00053837).rc, Rc.Pass)
        self.assertEqual(self.tx(0, WR_VF, 0, 0, 0xFF65B439).rc, Rc.Pass)
        # the domain-2 (ring) word may carry zero fields: the knob is about the per-core word
        self.assertEqual(self.tx(0, WR_VF, int(Domain.RING), 0, 0x28).rc, Rc.Pass)

    def test_on_a_zero_0x1d_is_rejected_too(self) -> None:
        self.use(reject_zero_fields=True)
        self.assertEqual(self.tx(0, WR_FAV, 0, 0, 0), Response(Rc.InvalidParam, 0))
        self.assertEqual(self.tx(0, RD_FAV).data, 0x3939)
        self.use()
        self.assertEqual(self.tx(0, WR_FAV, 0, 0, 0).rc, Rc.Pass)

    def test_the_check_order_puts_the_zero_fields_after_the_per_core_enable(self) -> None:
        self.use(reject_zero_fields=True, misc_global_config=0)  # per-core enable clear
        self.assertEqual(self.tx(1, WR_VF, 0, 0, 0).rc, Rc.OCLocked)


class FavoredAndGranularWrites(Rig):
    def test_0x1d_replaces_only_the_low_byte(self) -> None:
        self.assertEqual(self.tx(8, RD_FAV).data, 0x2E30)
        self.assertEqual(self.tx(8, WR_FAV, 0, 0, 0x2F), Response(Rc.Pass, 0x2F))
        self.assertEqual(self.tx(8, RD_FAV).data, 0x2E2F)  # [15:8] fused unchanged; [7:0] = the written ratio ([UNVERIFIED] on hardware)
        self.assertEqual(self.port.favored_word(8), 0x2E2F)
        self.assertEqual(self.tx(9, RD_FAV).data, 0x2E30)  # per CPU
        self.assertEqual(self.port.favored_word(0), 0x3939)

    def test_0x1d_rules(self) -> None:
        self.assertEqual(self.tx(0, WR_FAV, 0, 0, 0x100).rc, Rc.InvalidParam)  # not a ratio byte
        self.assertEqual(self.tx(0, WR_FAV, 0, 0, 63).rc, Rc.RatioExceedsLimits)  # SIM-ONLY P cap is 62
        self.assertEqual(self.tx(8, WR_FAV, 0, 0, 53).rc, Rc.RatioExceedsLimits)  # SIM-ONLY E cap is 52
        self.assertEqual(self.tx(0, WR_FAV, 1, 0, 0x39).rc, Rc.InvalidParam)
        self.assertEqual(self.tx(0, WR_FAV, 0, 1, 0x39).rc, Rc.InvalidParam)
        self.assertEqual(self.port.favored_word(0), 0x3939)  # nothing of the above changed it
        self.use(oc_locked=True)
        self.assertEqual(self.tx(0, WR_FAV, 0, 0, 0x39).rc, Rc.OCLocked)

    def test_0x27_stores_the_word_0x26_returns(self) -> None:
        self.assertEqual(self.tx(2, WR_GRAN, 0, 0, 0x205), Response(Rc.Pass, 0x205))
        self.assertEqual(self.tx(2, RD_GRAN).data, 0x205)
        self.assertEqual(self.port.granular_word(2), 0x205)
        self.assertEqual(self.tx(3, RD_GRAN).data, 3)  # per CPU

    def test_0x27_rules(self) -> None:
        self.assertEqual(self.tx(2, WR_GRAN, 0, 0, 0x10000).rc, Rc.InvalidParam)
        self.assertEqual(self.tx(2, WR_GRAN, 1, 0, 3).rc, Rc.InvalidParam)
        self.assertEqual(self.tx(2, RD_GRAN, 0, 1).rc, Rc.InvalidParam)
        self.use(oc_locked=True)
        self.assertEqual(self.tx(2, WR_GRAN, 0, 0, 3).rc, Rc.OCLocked)

    def test_silent_ignore_covers_0x1d_and_0x27(self) -> None:
        self.port.silent_ignore(WR_FAV)
        self.port.silent_ignore(WR_GRAN)
        self.assertEqual(self.tx(0, WR_FAV, 0, 0, 0x30), Response(Rc.Pass, 0x30))  # looks like a success ...
        self.assertEqual(self.tx(0, RD_FAV).data, 0x3939)  # ... but nothing changed
        self.assertEqual(self.tx(0, WR_GRAN, 0, 0, 0x7), Response(Rc.Pass, 0x7))
        self.assertEqual(self.tx(0, RD_GRAN).data, 3)
        with self.assertRaises(ValueError):
            self.port.silent_ignore(int(Cmd.ICC_MAX_WRITE))

    def test_force_rc_applies_to_the_new_commands(self) -> None:
        for cmd in (WR_FAV, WR_GRAN, RD_GRAN, RD_FAV, RD_FUSED):
            self.port.force_rc(cmd, int(Rc.FIVRError))
            self.assertEqual(self.tx(0, cmd, 0x11 if cmd == RD_FUSED else 0, 0, 0 if cmd in (RD_GRAN, RD_FAV, RD_FUSED) else 3), Response(Rc.FIVRError, 0))


class PerCpuLimitsAndKnobs(Rig):
    def test_the_v_target_limit_of_a_core_is_its_0x28_answer(self) -> None:
        self.assertEqual(self.tx(8, WR_VF, 0, 0, VfWord(48, 0x56D, False, 0).to_u32()).rc, Rc.Pass)
        self.assertEqual(self.tx(8, WR_VF, 0, 0, VfWord(48, 0x56E, False, 0).to_u32()).rc, Rc.VoltageExceedsLimit)
        self.assertEqual(self.tx(0, WR_VF, 0, 0, VfWord(57, 0x681, False, 0).to_u32()).rc, Rc.Pass)
        self.assertEqual(self.tx(0, WR_VF, 0, 0, VfWord(57, 0x682, False, 0).to_u32()).rc, Rc.VoltageExceedsLimit)
        # a domain-0 limit never leaks into another domain (their 0x28 stays the global SIM-ONLY max_voltage_raw)
        self.assertEqual(self.tx(0, RD_MAXV, int(Domain.RING), 0).data, 0x800)

    def test_the_e_cluster_wide_knob_still_works_on_the_measured_machine(self) -> None:
        self.use(e_cluster_wide=True)
        self.tx(9, WR_VF, 0, 0, 0xFF64A030)
        self.assertEqual({self.tx(c, RD_VF).data for c in (8, 9, 10, 11)}, {0xFF64A030})
        self.assertEqual(self.tx(12, RD_VF).data, 0)
        self.use(e_cluster_wide=False)
        self.tx(9, WR_VF, 0, 0, 0xFF64A030)
        self.assertEqual([self.tx(c, RD_VF).data for c in (8, 9, 10, 11)], [0, 0xFF64A030, 0, 0])

    def test_floors_still_override_the_fused_point_of_domain_0(self) -> None:
        self.use(floors={0: 0x400})
        self.assertEqual(self.tx(0, RD_FUSED, 0x11, 0).data, 0x400)
        self.assertEqual(self.tx(1, RD_FUSED, 0x11, 0).data, 0x400)

    def test_overrides_replace_fields_of_measured(self) -> None:
        cfg = SimConfig.measured(oc_locked=True)
        self.assertTrue(cfg.oc_locked)
        self.assertEqual(cfg.misc_global_config, 0x8)
        base = SimConfig.measured()
        other = SimConfig.measured()
        other.fused_point[0] = 1
        self.assertEqual(base.fused_point[0], 0x5B439)  # each call builds fresh dicts
        with self.assertRaises(TypeError):
            SimConfig.measured(no_such_field=1)

    def test_accessors_validate_the_cpu(self) -> None:
        for peek in (self.port.favored_word, self.port.fused_word, self.port.granular_word):
            with self.assertRaises(ValueError):
                peek(24)
            with self.assertRaises(ValueError):
                peek(-1)


class RaiseInsideThePcodeModel(Rig):
    """D-016 / T-022: a raise is just another word to the simulator -- the pcode MODEL (``oc_cap_ratio`` per core kind, the per-CPU ``0x28``
    voltage limit, both configurable and read live) decides whether it is accepted. These limits are SIM-ONLY test knobs: whether silicon
    accepts a raise is ``[UNVERIFIED]``, and a guard policy may allow more than the model (the sanity bound for E is ratio 60, the model
    default cap 52): the model then answers with the completion code, which ``controls`` reports and stops on."""

    E_RAISE = VfWord(49, 0x4A0 + 20, False, 0).to_u32()  # E cluster 0: 48 -> 49 with +20 raw on the fused V target

    def test_a_raise_inside_the_model_is_accepted_and_read_back_with_zero_fields_rejected(self) -> None:
        self.use(reject_zero_fields=True)
        self.assertEqual(self.tx(8, WR_VF, 0, 0, self.E_RAISE).rc, Rc.Pass)
        self.assertEqual(self.tx(8, RD_VF).data, self.E_RAISE)
        self.assertEqual(self.tx(8, WR_FAV, 0, 0, 49).rc, Rc.Pass)
        self.assertEqual(self.tx(8, RD_FAV).data & 0xFF, 49)
        self.assertEqual(self.tx(8, WR_VF, 0, 0, VfWord(49, 0, False, 0).to_u32()).rc, Rc.InvalidParam)  # still the unmeasured zero-field rule

    def test_the_ratio_cap_is_a_configurable_model_value(self) -> None:
        self.use(oc_cap_ratio={"P": 62, "E": 48})
        self.assertEqual(self.tx(8, WR_VF, 0, 0, self.E_RAISE).rc, Rc.RatioExceedsLimits)  # the model refuses what a policy may allow
        self.assertEqual(self.tx(8, WR_FAV, 0, 0, 49).rc, Rc.RatioExceedsLimits)
        self.assertEqual(self.tx(8, RD_VF).data, 0)  # nothing changed
        self.port.config.oc_cap_ratio["E"] = 52  # read live: raising the model cap lets the same word through
        self.assertEqual(self.tx(8, WR_VF, 0, 0, self.E_RAISE).rc, Rc.Pass)

    def test_the_voltage_limit_is_the_per_cpu_0x28_answer_and_configurable(self) -> None:
        self.use(max_voltage_cpu={c: 0x4B0 for c in (8, 9, 10, 11)})  # raw 1200: a V target above it is refused ...
        self.assertEqual(self.tx(8, RD_MAXV, 0, 0).data, 0x4B0)  # ... and that is exactly what the guard reads as the hardware maximum (minus 1)
        self.assertEqual(self.tx(8, WR_VF, 0, 0, VfWord(48, 0x4B0, False, 0).to_u32()).rc, Rc.Pass)
        self.assertEqual(self.tx(8, WR_VF, 0, 0, VfWord(48, 0x4B1, False, 0).to_u32()).rc, Rc.VoltageExceedsLimit)

    def test_the_default_model_leaves_room_for_the_example_policy(self) -> None:
        # the example policy's numbers (E ratio 50, 1.250 V; P ratio 58) are inside the measured machine's model caps
        self.assertEqual(self.tx(8, WR_VF, 0, 0, VfWord(50, VfWord.v_to_raw(1.25), False, 0).to_u32()).rc, Rc.Pass)
        self.assertEqual(self.tx(1, WR_VF, 0, 0, VfWord(58, 0x538, False, 0).to_u32()).rc, Rc.Pass)


class ConfigValidation(unittest.TestCase):
    def test_fixed_reads_may_not_shadow_writes_or_per_cpu_reads(self) -> None:
        for cmd in (0x11, 0x15, 0x1D, 0x27, 0x10, 0x14, 0x1C, 0x26, 0x09, 0x28):
            with self.subTest(cmd=hex(cmd)), self.assertRaises(ValueError):
                SimConfig(fixed_reads={(cmd, 0, 0): 1})

    def test_bad_words_and_keys_are_rejected(self) -> None:
        for kwargs in (
            {"fused_point": {0: -1}},
            {"favored_word": {0: 1 << 32}},
            {"granular_word": {0: True}},
            {"max_voltage_cpu": {0: "x"}},
            {"granular_default": 1.5},
            {"fixed_reads": {(0x04, 0, 0): 1 << 32}},
            {"fixed_reads": {(0x04, 0): 1}},
            {"fixed_reads": {(0x04, 0, 256): 1}},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises((ValueError, TypeError)):
                SimConfig(**kwargs)

    def test_old_defaults_are_unchanged(self) -> None:
        port = SimMsrPort()
        self.assertEqual(port.vf(4), VfWord(55, 0x544, False, 0))
        self.assertEqual(port.fused_word(4), 55 | 0x544 << 8)  # the SIM-ONLY stock point = the default word
        self.assertEqual(port.favored_word(4), 55 | 55 << 8)
        self.assertEqual(port.favored_word(8), 46 << 8 | 48)
        self.assertEqual(port.granular_word(4), 3)
        self.assertEqual(dataclasses.replace(SimConfig(), granular_default=5).granular_default, 5)


if __name__ == "__main__":
    unittest.main()
