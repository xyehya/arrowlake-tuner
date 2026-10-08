"""Tests for arltuner.msr.sim (T-011). The simulator is a test double: its defaults are SIM-ONLY values, asserted here
only to pin the simulator's own behaviour, never as hardware facts."""
from __future__ import annotations

import errno
import unittest

from arltuner.mailbox import MSR_OC_MAILBOX, Cmd, Domain, Rc, Response, VfWord, is_busy, pack, unpack
from arltuner.msr import MailboxTimeout, MailboxTransport, MsrError, MsrPort, SimConfig, SimMsrPort, SimRequest
from arltuner.msr.port import MAX_CPU

from ._support import BUSY, FakeClock

RD_VF, WR_VF = int(Cmd.VOLTAGE_FREQ_READ), int(Cmd.VOLTAGE_FREQ_WRITE)
RD_MISC, WR_MISC = int(Cmd.MISC_GLOBAL_CONFIG_READ), int(Cmd.MISC_GLOBAL_CONFIG_WRITE)
PER_CORE_ENABLE = 0x8


class SimCase(unittest.TestCase):
    def setUp(self):
        self.port = SimMsrPort()
        self.fc = FakeClock()

    def use(self, **cfg):
        self.port = SimMsrPort(SimConfig(**cfg))
        return self.port

    def tx(self, cpu, cmd, p1=0, p2=0, data=0) -> Response:
        transport = MailboxTransport(self.port, sleep=self.fc.sleep, clock=self.fc.clock, timeout_s=1.0)
        return transport.transact(cpu, cmd, p1, p2, data)

    def word(self, cpu, domain=0, p2=0) -> VfWord:
        r = self.tx(cpu, RD_VF, domain, p2)
        self.assertEqual(r.rc, Rc.Pass)
        return VfWord.from_u32(r.data)

    def enable_per_core(self, cpu=0):
        self.assertEqual(self.tx(cpu, WR_MISC, 0, 0, PER_CORE_ENABLE).rc, Rc.Pass)

    def write_vf(self, cpu, word: VfWord, domain=0, p2=0) -> Response:
        return self.tx(cpu, WR_VF, domain, p2, word.to_u32())


class TestGoldenSequences(SimCase):
    def test_read_p_core_defaults(self):
        w = self.word(4)
        self.assertEqual((w.ratio, w.v_target_raw, w.static, w.offset_raw), (55, 0x544, False, 0))
        for cpu in (0, 3):
            w = self.word(cpu)
            self.assertEqual((w.ratio, w.v_target_raw), (57, 0x5B4))
        self.assertEqual(self.tx(4, RD_VF, 0, 0).data, 55 | (0x544 << 8))

    def test_read_e_core_defaults(self):
        for cpu in (8, 9, 10, 11):
            w = self.word(cpu)
            self.assertEqual((w.ratio, w.v_target_raw), (48, 0x4A0), cpu)
        for cpu in (12, 15, 16, 23):
            w = self.word(cpu)
            self.assertEqual((w.ratio, w.v_target_raw), (48, 0x48C), cpu)

    def test_enable_per_core_then_undervolt_50mv_reads_back(self):
        self.assertEqual(self.tx(4, RD_MISC), Response(Rc.Pass, 0))
        self.assertEqual(self.tx(4, WR_MISC, 0, 0, PER_CORE_ENABLE), Response(Rc.Pass, PER_CORE_ENABLE))
        self.assertEqual(self.tx(4, RD_MISC), Response(Rc.Pass, PER_CORE_ENABLE))
        self.assertTrue(self.port.per_core_enabled)
        new = self.word(4).with_(offset_raw=VfWord.mv_to_offset_raw(-50))  # -50 mV -> raw -51 (registers.md)
        self.assertEqual(new.to_u32(), 0xF9A00000 | (0x544 << 8) | 55)
        r = self.tx(4, WR_VF, 0, 0, new.to_u32())
        self.assertEqual(r, Response(Rc.Pass, new.to_u32()))  # data = the stored word
        self.assertEqual(self.tx(4, RD_VF, 0, 0).data, new.to_u32())
        self.assertEqual(self.word(4), new)

    def test_write_before_enable_is_oclocked_and_state_unchanged(self):
        before = self.word(4)
        r = self.write_vf(4, before.with_(offset_raw=-51))
        self.assertEqual(r.rc, Rc.OCLocked)
        self.assertEqual(self.word(4), before)

    def test_per_core_enable_is_package_wide(self):
        self.enable_per_core(cpu=1)  # enabled through CPU 1 ...
        self.assertEqual(self.write_vf(9, self.word(9).with_(offset_raw=-10)).rc, Rc.Pass)  # ... seen by CPU 9
        self.assertEqual(self.tx(20, RD_MISC).data, PER_CORE_ENABLE)

    def test_writes_affect_only_the_addressed_cpu_for_p_cores(self):
        self.enable_per_core()
        self.write_vf(4, self.word(4).with_(ratio=56))
        self.assertEqual(self.word(4).ratio, 56)
        self.assertEqual(self.word(5).ratio, 55)
        self.assertEqual(self.word(0).ratio, 57)

    def test_requires_enable_can_be_disabled_by_config(self):
        self.use(per_core_requires_enable=False)
        self.assertEqual(self.write_vf(4, self.word(4).with_(offset_raw=-5)).rc, Rc.Pass)

    def test_misc_write_data_is_stored_whole(self):
        self.tx(0, WR_MISC, 0, 0, 0xA)
        self.assertEqual(self.tx(7, RD_MISC).data, 0xA)
        self.assertEqual(self.port.misc_global_config, 0xA)

    def test_initial_misc_config_from_config(self):
        self.use(misc_global_config=PER_CORE_ENABLE)
        self.assertTrue(self.port.per_core_enabled)
        self.assertEqual(self.write_vf(4, self.word(4).with_(offset_raw=-1)).rc, Rc.Pass)


class TestVfRules(SimCase):
    def setUp(self):
        super().setUp()
        self.enable_per_core()

    def test_ratio_above_cap_p(self):
        base = self.word(4)
        self.assertEqual(self.write_vf(4, base.with_(ratio=62)).rc, Rc.Pass)  # cap itself is allowed
        self.assertEqual(self.write_vf(4, base.with_(ratio=63)).rc, Rc.RatioExceedsLimits)
        self.assertEqual(self.write_vf(4, base.with_(ratio=70)).rc, Rc.RatioExceedsLimits)
        self.assertEqual(self.word(4).ratio, 62)  # the rejected writes changed nothing

    def test_ratio_cap_is_per_core_kind(self):
        base = self.word(9)
        self.assertEqual(self.write_vf(9, base.with_(ratio=52)).rc, Rc.Pass)
        self.assertEqual(self.write_vf(9, base.with_(ratio=53)).rc, Rc.RatioExceedsLimits)
        self.assertEqual(self.write_vf(4, self.word(4).with_(ratio=53)).rc, Rc.Pass)  # 53 is fine on a P core

    def test_ratio_cap_is_configurable(self):
        self.use(oc_cap_ratio={"P": 58, "E": 50}, per_core_requires_enable=False)
        self.assertEqual(self.write_vf(4, self.word(4).with_(ratio=59)).rc, Rc.RatioExceedsLimits)
        self.assertEqual(self.write_vf(4, self.word(4).with_(ratio=58)).rc, Rc.Pass)

    def test_voltage_target_limit(self):
        self.use(max_voltage_raw=0x600, per_core_requires_enable=False)
        base = self.word(4)
        self.assertEqual(self.write_vf(4, base.with_(v_target_raw=0x600)).rc, Rc.Pass)
        self.assertEqual(self.write_vf(4, base.with_(v_target_raw=0x601)).rc, Rc.VoltageExceedsLimit)
        self.assertEqual(self.word(4).v_target_raw, 0x600)

    def test_default_voltage_limit_is_2v(self):
        self.assertEqual(self.write_vf(4, self.word(4).with_(v_target_raw=0x800)).rc, Rc.Pass)
        self.assertEqual(self.write_vf(4, self.word(4).with_(v_target_raw=0x801)).rc, Rc.VoltageExceedsLimit)

    def test_undervolt_protection_blocks_negative_offsets_only(self):
        self.use(uvp=True, per_core_requires_enable=False)
        base = self.word(4)
        self.assertEqual(self.write_vf(4, base.with_(offset_raw=-51)).rc, Rc.UndervoltProtection)
        self.assertEqual(self.write_vf(4, base.with_(offset_raw=-1)).rc, Rc.UndervoltProtection)
        self.assertEqual(self.word(4).offset_raw, 0)
        self.assertEqual(self.write_vf(4, base.with_(offset_raw=0)).rc, Rc.Pass)
        self.assertEqual(self.write_vf(4, base.with_(offset_raw=26)).rc, Rc.Pass)

    def test_without_uvp_negative_offsets_pass(self):
        self.assertEqual(self.write_vf(4, self.word(4).with_(offset_raw=-1024)).rc, Rc.Pass)
        self.assertEqual(self.word(4).offset_raw, -1024)

    def test_oc_locked_refuses_every_vf_write_and_the_enable_write(self):
        self.use(oc_locked=True, misc_global_config=PER_CORE_ENABLE)
        self.assertEqual(self.write_vf(4, self.word(4).with_(offset_raw=-5)).rc, Rc.OCLocked)
        self.assertEqual(self.write_vf(4, self.word(4), domain=2).rc, Rc.OCLocked)  # ring too
        self.assertEqual(self.tx(4, WR_MISC, 0, 0, PER_CORE_ENABLE).rc, Rc.OCLocked)  # XTU log: SET 0x15 data 8 RC 1
        self.assertEqual(self.tx(4, RD_VF, 0, 0).rc, Rc.Pass)  # reads still work
        self.assertEqual(self.word(4).offset_raw, 0)

    def test_oc_locked_xtu_log_scenario_leaves_enable_bit_clear(self):
        self.use(oc_locked=True)
        self.assertEqual(self.tx(4, WR_MISC, 0, 0, PER_CORE_ENABLE).rc, Rc.OCLocked)
        self.assertEqual(self.tx(4, RD_MISC).data, 0)

    def test_static_mode_allowed_only_if_configured(self):
        static = self.word(4).with_(static=True)
        self.assertEqual(self.write_vf(4, static).rc, Rc.Pass)  # default allows it
        self.assertTrue(self.word(4).static)
        self.use(allow_static=False, per_core_requires_enable=False)
        r = self.write_vf(4, static)
        self.assertEqual(r.rc, Rc.InvalidParam)
        self.assertFalse(self.word(4).static)

    def test_check_order_oclocked_then_ratio_then_voltage_then_uvp(self):
        self.use(uvp=True, max_voltage_raw=0x600, per_core_requires_enable=False)
        bad = VfWord(ratio=70, v_target_raw=0x700, static=False, offset_raw=-10)
        self.assertEqual(self.write_vf(4, bad).rc, Rc.RatioExceedsLimits)
        self.assertEqual(self.write_vf(4, bad.with_(ratio=55)).rc, Rc.VoltageExceedsLimit)
        self.assertEqual(self.write_vf(4, bad.with_(ratio=55, v_target_raw=0x544)).rc, Rc.UndervoltProtection)

    def test_config_is_read_live(self):
        self.assertEqual(self.write_vf(4, self.word(4).with_(offset_raw=-5)).rc, Rc.Pass)
        self.port.config.uvp = True
        self.assertEqual(self.write_vf(4, self.word(4).with_(offset_raw=-6)).rc, Rc.UndervoltProtection)


class TestOtherDomains(SimCase):
    def test_default_domain_words_read(self):
        ring = self.word(0, Domain.RING)
        self.assertEqual((ring.ratio, ring.v_target_raw), (40, 0x4C8))  # trace words in registers.md
        ngu = self.word(0, Domain.NGU)
        self.assertEqual((ngu.ratio, ngu.v_target_raw), (34, 0x4BD))
        self.assertEqual(self.word(0, Domain.GT).ratio, 40)

    def test_domain_state_is_shared_across_cpus_and_needs_no_per_core_enable(self):
        r = self.write_vf(0, self.word(0, Domain.RING).with_(ratio=45), domain=Domain.RING)
        self.assertEqual(r.rc, Rc.Pass)
        self.assertEqual(self.word(17, Domain.RING).ratio, 45)
        self.assertEqual(self.word(4).ratio, 55)  # core state untouched

    def test_domain_ratio_cap(self):
        self.assertEqual(self.write_vf(0, VfWord(50, 0x4C8, False, 0), domain=Domain.RING).rc, Rc.Pass)
        self.assertEqual(self.write_vf(0, VfWord(51, 0x4C8, False, 0), domain=Domain.RING).rc, Rc.RatioExceedsLimits)
        self.assertEqual(self.write_vf(0, VfWord(41, 0x4BD, False, 0), domain=Domain.NGU).rc, Rc.RatioExceedsLimits)

    def test_unsupported_domains(self):
        for domain in (Domain.GT_UNSLICE, Domain.SA, Domain.MEMSS, 5, 99):
            for cmd in (RD_VF, WR_VF):
                with self.subTest(domain=domain, cmd=cmd):
                    self.assertEqual(self.tx(0, cmd, int(domain), 0, 0).rc, Rc.UnsupportedDomain)

    def test_initial_overrides(self):
        self.use(
            initial_vf={4: VfWord(50, 0x500, False, 0)},
            initial_domain_vf={int(Domain.RING): VfWord(30, 0x400, False, 0)},
        )
        self.assertEqual(self.word(4).ratio, 50)
        self.assertEqual(self.word(5).ratio, 55)
        self.assertEqual(self.word(0, Domain.RING).ratio, 30)


class TestPerPoint(SimCase):
    def test_p2_nonzero_is_invalid_param_by_default(self):
        self.assertEqual(self.tx(4, RD_VF, 0, 1).rc, Rc.InvalidParam)
        self.assertEqual(self.tx(4, WR_VF, 0, 4, 0x30).rc, Rc.InvalidParam)
        self.assertEqual(self.port.vf(4, 0, 4), VfWord(0, 0, False, 0))

    def test_per_point_keeps_state_per_cpu_and_point(self):
        self.use(per_point=True)
        self.enable_per_core()
        offset_only = VfWord(ratio=0x30, v_target_raw=0, static=False, offset_raw=26)  # trace word 0x03400030 (+25.39 mV)
        self.assertEqual(offset_only.to_u32(), 0x0340_0030)
        self.assertEqual(self.write_vf(4, offset_only, p2=4).rc, Rc.Pass)
        self.assertEqual(self.word(4, p2=4), offset_only)
        self.assertEqual(self.word(4, p2=8), VfWord(0, 0, False, 0))  # other point untouched
        self.assertEqual(self.word(5, p2=4), VfWord(0, 0, False, 0))  # other CPU untouched
        self.assertEqual(self.word(4).offset_raw, 0)  # the whole-core word (p2=0) is untouched

    def test_per_point_bounds(self):
        self.use(per_point=True)
        self.assertEqual(self.tx(4, RD_VF, 0, 15).rc, Rc.Pass)
        self.assertEqual(self.tx(4, RD_VF, 0, 16).rc, Rc.InvalidParam)

    def test_per_point_rules_still_apply(self):
        self.use(per_point=True)
        self.assertEqual(self.write_vf(4, VfWord(0, 0, False, 26), p2=4).rc, Rc.OCLocked)  # no enable yet


class TestEClusterScope(SimCase):
    def test_cluster_wide_off_changes_only_the_addressed_e_cpu(self):
        self.enable_per_core()
        self.write_vf(9, self.word(9).with_(offset_raw=-30))
        self.assertEqual(self.word(9).offset_raw, -30)
        for cpu in (8, 10, 11, 12):
            self.assertEqual(self.word(cpu).offset_raw, 0, cpu)

    def test_cluster_wide_on_updates_all_four_cpus_of_the_cluster(self):
        self.use(e_cluster_wide=True)
        self.enable_per_core()
        r = self.write_vf(9, self.word(9).with_(offset_raw=-30, v_target_raw=0x4A0))
        self.assertEqual(r.rc, Rc.Pass)
        for cpu in (8, 9, 10, 11):
            self.assertEqual(self.word(cpu).offset_raw, -30, cpu)
        for cpu in (12, 15, 16, 23, 0, 4):
            self.assertEqual(self.word(cpu).offset_raw, 0, cpu)

    def test_cluster_wide_does_not_apply_to_p_cores(self):
        self.use(e_cluster_wide=True)
        self.enable_per_core()
        self.write_vf(4, self.word(4).with_(offset_raw=-30))
        self.assertEqual([self.word(c).offset_raw for c in range(8)], [0, 0, 0, 0, -30, 0, 0, 0])

    def test_rejected_write_changes_no_cluster_member(self):
        self.use(e_cluster_wide=True)
        self.enable_per_core()
        self.assertEqual(self.write_vf(9, self.word(9).with_(ratio=90)).rc, Rc.RatioExceedsLimits)
        self.assertEqual([self.word(c).ratio for c in (8, 9, 10, 11)], [48] * 4)


class TestCapabilityAndReads(SimCase):
    def test_capability_p1_3_is_oc_not_supported(self):
        r = self.tx(0, int(Cmd.OC_CAPABILITY_READ), int(Domain.GT_UNSLICE), 0)
        self.assertEqual(r, Response(Rc.OCNotSupported, 0))  # XTU log: GET 0x01 P1=3 RC 5

    def test_capability_core_domain_by_kind(self):
        p = self.tx(4, int(Cmd.OC_CAPABILITY_READ), 0, 0)
        e = self.tx(9, int(Cmd.OC_CAPABILITY_READ), 0, 0)
        self.assertEqual((p.rc, p.data & 0xFF, p.data >> 8), (Rc.Pass, 62, 0b111))
        self.assertEqual((e.rc, e.data & 0xFF, e.data >> 8), (Rc.Pass, 52, 0b111))

    def test_capability_flags_configurable_and_other_domains(self):
        self.use(capability_flags=0b101)
        self.assertEqual(self.tx(4, int(Cmd.OC_CAPABILITY_READ), 0, 0).data >> 8, 0b101)
        r = self.tx(4, int(Cmd.OC_CAPABILITY_READ), int(Domain.RING), 0)
        self.assertEqual((r.rc, r.data & 0xFF), (Rc.Pass, 50))
        self.assertEqual(self.tx(4, int(Cmd.OC_CAPABILITY_READ), int(Domain.SA), 0).rc, Rc.UnsupportedDomain)
        self.assertEqual(self.tx(4, int(Cmd.OC_CAPABILITY_READ), 0, 1).rc, Rc.InvalidParam)

    def test_per_core_capability_returns_fixed_word(self):
        self.assertEqual(self.tx(0, int(Cmd.PER_CORE_CAPABILITY_READ), 0, 0), Response(Rc.Pass, 0x39393939))
        self.assertEqual(self.tx(0, int(Cmd.PER_CORE_CAPABILITY_READ), 2, 1).rc, Rc.Pass)
        self.assertEqual(self.tx(0, int(Cmd.PER_CORE_CAPABILITY_READ), 0, 2).rc, Rc.InvalidParam)

    def test_max_voltage_limit(self):
        self.assertEqual(self.tx(0, int(Cmd.MAXIMUM_VOLTAGE_LIMIT_READ), 0, 0), Response(Rc.Pass, 0x800))
        self.assertEqual(self.tx(0, int(Cmd.MAXIMUM_VOLTAGE_LIMIT_READ), 4, 0).rc, Rc.UnsupportedDomain)
        self.use(max_voltage_raw=0x65C)
        self.assertEqual(self.tx(0, int(Cmd.MAXIMUM_VOLTAGE_LIMIT_READ), 2, 0).data, 0x65C)

    def test_favored_core_read(self):
        read = lambda cpu: self.tx(cpu, int(Cmd.FAVORED_CORE_READ), 0, 0)  # noqa: E731
        self.assertEqual(read(0), Response(Rc.Pass, 57 | 57 << 8))
        self.assertEqual(read(3).data >> 8 & 0xFF, 57)
        self.assertEqual(read(4).data, 55 | 55 << 8)
        self.assertEqual(read(8).data, 46 << 8 | 48)  # SIM-ONLY: fused max E 46x in [15:8], favored ratio = the default word's 48x in [7:0]
        self.assertEqual(self.tx(0, int(Cmd.FAVORED_CORE_READ), 1, 0).rc, Rc.InvalidParam)

    def test_floors_default_zero_unless_configured(self):
        # domain 0 (P2=0) answers the per-CPU fused V/F point (T-021); the other domains answer 0 unless configured
        self.assertEqual(self.tx(0, int(Cmd.VOLTAGE_FLOOR_READ), 0x11, 0), Response(Rc.Pass, 57 | 0x5B4 << 8))
        self.assertEqual(self.tx(0, int(Cmd.VOLTAGE_FLOOR_READ), 0x11, 2), Response(Rc.Pass, 0))
        self.assertEqual(self.tx(0, int(Cmd.VOLTAGE_FLOOR_READ), 0x11, 5).rc, Rc.UnsupportedDomain)
        self.assertEqual(self.tx(0, int(Cmd.VOLTAGE_FLOOR_READ), 0x21, 0).rc, Rc.IllegalCommandSelectedInParam1)
        self.use(floors={0: 0x0000_0400})
        self.assertEqual(self.tx(0, int(Cmd.VOLTAGE_FLOOR_READ), 0x11, 0).data, 0x400)

    def test_unrecognized_commands(self):
        for cmd in (0x00, 0x03, 0x04, 0x05, 0x16, 0x18, 0x1A, 0x7F, 0xFF):  # 0x1D/0x26/0x27 are modelled since T-021
            with self.subTest(cmd=cmd):
                self.assertEqual(self.tx(0, cmd, 0, 0, 0), Response(Rc.UnrecognizedCommand, 0))

    def test_p2_must_be_zero_on_single_word_commands(self):
        for cmd in (RD_MISC, int(Cmd.FAVORED_CORE_READ), int(Cmd.MAXIMUM_VOLTAGE_LIMIT_READ)):
            with self.subTest(cmd=cmd):
                self.assertEqual(self.tx(0, cmd, 0, 3, 0).rc, Rc.InvalidParam)
        self.assertEqual(self.tx(0, WR_MISC, 1, 0, 8).rc, Rc.InvalidParam)
        self.assertEqual(self.tx(0, RD_MISC, 0, 0).data, 0)  # the rejected write changed nothing


class TestProtocolModel(SimCase):
    def test_initial_mailbox_reads_idle_zero(self):
        self.assertEqual(self.port.rdmsr(4, MSR_OC_MAILBOX), 0)

    def test_busy_polls_then_response_persists(self):
        self.use(busy_polls=3)
        self.port.wrmsr(4, MSR_OC_MAILBOX, pack(RD_VF, 0, 0, 0))
        reads = [self.port.rdmsr(4, MSR_OC_MAILBOX) for _ in range(6)]
        self.assertEqual([is_busy(r) for r in reads], [True, True, True, False, False, False])
        self.assertEqual(len(set(reads[3:])), 1)  # last response persists
        self.assertEqual(unpack(reads[3]), Response(Rc.Pass, 55 | 0x544 << 8))
        self.assertEqual(self.port.busy_reads, 3)
        self.assertEqual(reads[0] & ~BUSY, pack(RD_VF, 0, 0, 0) & ~BUSY)  # busy reads still show the latched request

    def test_transact_read_count_follows_busy_polls(self):
        for polls in (0, 1, 4):
            with self.subTest(polls=polls):
                port = self.use(busy_polls=polls)
                self.tx(4, RD_VF, 0, 0)
                self.assertEqual(port.rdmsr_count, 1 + (polls + 1))  # idle read + (busy reads + the completing read)
                self.assertEqual(port.wrmsr_count, 1)

    def test_response_word_layout(self):
        self.port.wrmsr(2, MSR_OC_MAILBOX, pack(int(Cmd.OC_CAPABILITY_READ), 3, 0, 0))
        self.port.rdmsr(2, MSR_OC_MAILBOX)  # busy
        raw = self.port.rdmsr(2, MSR_OC_MAILBOX)
        self.assertEqual(raw, 0x05 << 32)  # (rc << 32) | data with bit 63 clear

    def test_write_while_busy_is_eio_and_not_latched(self):
        self.port.wrmsr(4, MSR_OC_MAILBOX, pack(RD_VF, 0, 0, 0))
        with self.assertRaises(MsrError) as ctx:
            self.port.wrmsr(4, MSR_OC_MAILBOX, pack(RD_MISC, 0, 0, 0))
        self.assertEqual(ctx.exception.errno, errno.EIO)
        self.assertEqual(len(self.port.requests), 1)
        self.assertEqual(len(self.port.writes), 2)  # both attempts reached the device model

    def test_write_without_run_bit_is_eio(self):
        with self.assertRaises(MsrError) as ctx:
            self.port.wrmsr(4, MSR_OC_MAILBOX, pack(RD_VF, 0, 0, 0) & ~BUSY)
        self.assertEqual(ctx.exception.errno, errno.EIO)
        self.assertEqual(self.port.requests, [])

    def test_mailboxes_are_per_cpu(self):
        self.port.wrmsr(4, MSR_OC_MAILBOX, pack(RD_VF, 0, 0, 0))
        self.assertFalse(is_busy(self.port.rdmsr(5, MSR_OC_MAILBOX)))
        self.port.wrmsr(5, MSR_OC_MAILBOX, pack(RD_VF, 0, 0, 0))  # does not raise: CPU 5 is idle
        self.assertTrue(is_busy(self.port.rdmsr(4, MSR_OC_MAILBOX)))

    def test_responses_are_per_cpu(self):
        self.tx(4, RD_VF, 0, 0)
        self.assertEqual(self.port.rdmsr(5, MSR_OC_MAILBOX), 0)

    def test_busy_polls_zero_answers_immediately(self):
        self.use(busy_polls=0)
        self.port.wrmsr(4, MSR_OC_MAILBOX, pack(RD_VF, 0, 0, 0))
        self.assertFalse(is_busy(self.port.rdmsr(4, MSR_OC_MAILBOX)))

    def test_negative_busy_polls_rejected(self):
        with self.assertRaises(ValueError):
            SimConfig(busy_polls=-1)


class TestObservability(SimCase):
    def test_writes_requests_and_counters(self):
        self.enable_per_core(cpu=2)
        new = self.word(4).with_(offset_raw=-10)
        self.write_vf(4, new)
        self.assertEqual(
            self.port.writes,
            [
                (2, MSR_OC_MAILBOX, pack(WR_MISC, 0, 0, PER_CORE_ENABLE)),
                (4, MSR_OC_MAILBOX, pack(RD_VF, 0, 0, 0)),
                (4, MSR_OC_MAILBOX, pack(WR_VF, 0, 0, new.to_u32())),
            ],
        )
        self.assertEqual(
            self.port.requests,
            [
                SimRequest(2, WR_MISC, 0, 0, PER_CORE_ENABLE),
                SimRequest(4, RD_VF, 0, 0, 0),
                SimRequest(4, WR_VF, 0, 0, new.to_u32()),
            ],
        )
        self.assertEqual(self.port.wrmsr_count, 3)
        self.assertEqual(self.port.rdmsr_count, len(self.port.reads))
        self.assertTrue(all(msr == MSR_OC_MAILBOX for _, msr in self.port.reads))

    def test_state_snapshot_and_clear_log(self):
        s = self.port.state(4)
        self.assertEqual((s.cpu, s.vf.ratio, s.busy, s.busy_polls_left, s.response), (4, 55, False, 0, 0))
        self.port.wrmsr(4, MSR_OC_MAILBOX, pack(RD_VF, 0, 0, 0))
        self.assertTrue(self.port.state(4).busy)
        self.assertEqual(self.port.state(4).busy_polls_left, 1)
        self.port.clear_log()
        self.assertEqual((self.port.writes, self.port.reads, self.port.requests, self.port.busy_reads), ([], [], [], 0))
        self.assertTrue(self.port.state(4).busy)  # state survives a log clear
        with self.assertRaises(ValueError):
            self.port.state(99)


class TestOtherMsrsAndArguments(SimCase):
    def test_set_msr_then_rdmsr(self):
        self.port.set_msr(3, 0x1A2, 0x80691400)
        self.assertEqual(self.port.rdmsr(3, 0x1A2), 0x80691400)

    def test_unconfigured_msr_is_eio(self):
        with self.assertRaises(MsrError) as ctx:
            self.port.rdmsr(3, 0x1A2)
        self.assertEqual(ctx.exception.errno, errno.EIO)
        with self.assertRaises(MsrError) as ctx:
            self.port.wrmsr(3, 0x1A2, 1)
        self.assertEqual(ctx.exception.errno, errno.EIO)
        self.assertEqual(self.port.writes, [(3, 0x1A2, 1)])  # the attempt is still logged

    def test_configured_msr_is_writable_register(self):
        self.port.set_msr(3, 0x1AD, 1)
        self.port.wrmsr(3, 0x1AD, 0x37)
        self.assertEqual(self.port.rdmsr(3, 0x1AD), 0x37)

    def test_set_msr_rejects_the_mailbox_and_unknown_cpus(self):
        with self.assertRaises(ValueError):
            self.port.set_msr(0, MSR_OC_MAILBOX, 1)
        with self.assertRaises(ValueError):
            self.port.set_msr(99, 0x10, 1)

    def test_out_of_range_arguments_are_value_errors(self):
        for args in ((-1, 0x10), (MAX_CPU + 1, 0x10), (0, -1), (0, 2**32), (True, 0x10), (0, 1.5)):
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.port.rdmsr(*args)
        for args in ((0, 0x10, -1), (0, 0x10, 2**64), (0, 0x10, "1"), (-1, 0x10, 0)):
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.port.wrmsr(*args)
        self.assertEqual(self.port.writes, [])

    def test_cpu_outside_topology_is_enxio(self):
        with self.assertRaises(MsrError) as ctx:
            self.port.rdmsr(24, MSR_OC_MAILBOX)
        self.assertEqual(ctx.exception.errno, errno.ENXIO)
        with self.assertRaises(MsrError) as ctx:
            self.port.wrmsr(24, MSR_OC_MAILBOX, pack(RD_VF, 0, 0, 0))
        self.assertEqual(ctx.exception.errno, errno.ENXIO)
        self.assertEqual((self.port.writes, self.port.reads), ([], []))

    def test_read_only_config_refuses_writes_with_eperm_and_logs_nothing(self):
        self.use(writable=False)
        self.assertFalse(self.port.writable)
        with self.assertRaises(MsrError) as ctx:
            self.port.wrmsr(4, MSR_OC_MAILBOX, pack(RD_VF, 0, 0, 0))
        self.assertEqual(ctx.exception.errno, errno.EPERM)
        self.assertEqual((self.port.writes, self.port.requests), ([], []))
        self.assertEqual(self.port.rdmsr(4, MSR_OC_MAILBOX), 0)  # reads still work

    def test_is_an_msrport_and_writable_by_default(self):
        self.assertIsInstance(self.port, MsrPort)
        self.assertTrue(self.port.writable)


class TestFaultInjection(SimCase):
    def test_stuck_busy_before_write_makes_transport_abort_without_writing(self):
        self.port.stuck_busy(4, before_write=True)
        with self.assertRaises(MailboxTimeout) as ctx:
            self.tx(4, WR_VF, 0, 0, 0x1234)
        self.assertEqual(ctx.exception.args, ("busy before write",))
        self.assertEqual((self.port.writes, self.port.requests), ([], []))
        self.assertTrue(is_busy(self.port.rdmsr(4, MSR_OC_MAILBOX)))
        self.assertFalse(is_busy(self.port.rdmsr(5, MSR_OC_MAILBOX)))  # only that CPU

    def test_stuck_busy_before_write_makes_raw_write_fail_with_eio(self):
        self.port.stuck_busy(4, before_write=True)
        with self.assertRaises(MsrError) as ctx:
            self.port.wrmsr(4, MSR_OC_MAILBOX, pack(RD_VF, 0, 0, 0))
        self.assertEqual(ctx.exception.errno, errno.EIO)

    def test_stuck_busy_after_write_records_the_request_but_never_completes(self):
        self.enable_per_core(cpu=5)
        before = self.port.vf(4)
        self.port.stuck_busy(4)  # before_write=False is the default
        new = before.with_(offset_raw=-20)
        with self.assertRaises(MailboxTimeout) as ctx:
            self.tx(4, WR_VF, 0, 0, new.to_u32())
        self.assertEqual(ctx.exception.args, ("busy after write",))
        self.assertIn(SimRequest(4, WR_VF, 0, 0, new.to_u32()), self.port.requests)
        self.assertEqual(self.port.vf(4), before)  # never executed
        self.assertTrue(is_busy(self.port.rdmsr(4, MSR_OC_MAILBOX)))

    def test_clear_faults_recovers_a_hung_mailbox(self):
        self.port.stuck_busy(4)
        with self.assertRaises(MailboxTimeout):
            self.tx(4, RD_VF, 0, 0)
        self.port.clear_faults()
        self.assertEqual(self.tx(4, RD_VF, 0, 0).rc, Rc.Pass)

    def test_eio_on_a_specific_cpu_and_msr(self):
        self.port.set_msr(4, 0x1A2, 7)
        self.port.eio(4, 0x1A2)
        with self.assertRaises(MsrError) as ctx:
            self.port.rdmsr(4, 0x1A2)
        self.assertEqual(ctx.exception.errno, errno.EIO)
        self.port.set_msr(5, 0x1A2, 7)
        self.assertEqual(self.port.rdmsr(5, 0x1A2), 7)  # other CPU unaffected

    def test_eio_on_the_mailbox_propagates_through_the_transport(self):
        self.port.eio(4, MSR_OC_MAILBOX)
        with self.assertRaises(MsrError) as ctx:
            self.tx(4, RD_VF, 0, 0)
        self.assertEqual(ctx.exception.errno, errno.EIO)
        self.assertEqual(self.port.writes, [])  # the very first read failed; nothing was written
        self.assertEqual(self.tx(5, RD_VF, 0, 0).rc, Rc.Pass)

    def test_force_rc_overrides_the_model_and_leaves_state(self):
        self.enable_per_core()
        self.port.force_rc(WR_VF, int(Rc.FIVRError))
        before = self.word(4)
        self.assertEqual(self.write_vf(4, before.with_(offset_raw=-5)), Response(Rc.FIVRError, 0))
        self.assertEqual(self.word(4), before)
        self.assertEqual(self.tx(4, RD_VF, 0, 0).rc, Rc.Pass)  # other commands unaffected

    def test_silent_ignore_passes_but_changes_nothing(self):
        self.enable_per_core()
        self.port.silent_ignore(WR_VF)
        before = self.word(4)
        new = before.with_(offset_raw=-20)
        r = self.write_vf(4, new)
        self.assertEqual(r, Response(Rc.Pass, new.to_u32()))  # looks like a success ...
        self.assertEqual(self.word(4), before)  # ... but the read-back differs

    def test_silent_ignore_still_returns_error_codes(self):
        self.port.silent_ignore(WR_VF)
        self.assertEqual(self.write_vf(4, self.word(4)).rc, Rc.OCLocked)  # no per-core enable: the rule fires first

    def test_silent_ignore_misc_write(self):
        self.port.silent_ignore(WR_MISC)
        self.assertEqual(self.tx(4, WR_MISC, 0, 0, PER_CORE_ENABLE), Response(Rc.Pass, PER_CORE_ENABLE))
        self.assertFalse(self.port.per_core_enabled)

    def test_fault_arguments_are_validated(self):
        with self.assertRaises(ValueError):
            self.port.silent_ignore(int(Cmd.VOLTAGE_FREQ_READ))
        with self.assertRaises(ValueError):
            self.port.force_rc(256, 0)
        with self.assertRaises(ValueError):
            self.port.force_rc(0x11, -1)
        with self.assertRaises(ValueError):
            self.port.stuck_busy(99)


class TestVfPointModel(SimCase):
    """T-025: the V/F points of a domain-0 core. ``[MEASURED lnx 2026-10-02]`` (E CPU 8): ``0x10 P2=6`` = ``0x0000002E`` (46x, V target 0, offset 0),
    ``0x10 P2=7`` -> rc ``0x11``. The per-kind point counts (E 6 / P 8) are the SkatterBencher figure ``[COMMUNITY]``."""

    COUNTS = {"E": 6, "P": 8}

    def test_the_e_top_point_reads_the_measured_word_by_default(self):
        self.use(per_point=True)
        r = self.tx(8, RD_VF, 0, 6)
        self.assertEqual((r.rc, r.data), (Rc.Pass, 0x0000002E))
        self.assertEqual(self.word(9, p2=6), VfWord(46, 0, False, 0))  # every E CPU, same kind default
        self.assertEqual(self.word(8, p2=5), VfWord(0, 0, False, 0))  # no other point has a measured ratio: zero, as before T-025
        self.assertEqual(self.word(4, p2=6), VfWord(0, 0, False, 0))  # a P CPU: none measured

    def test_point_ratios_are_configurable_per_kind_and_point(self):
        self.use(per_point=True, point_ratios={("E", 1): 30, ("E", 6): 44, ("P", 8): 57})
        self.assertEqual(self.word(8, p2=1).ratio, 30)
        self.assertEqual(self.word(15, p2=6).ratio, 44)
        self.assertEqual(self.word(4, p2=8).ratio, 57)
        self.assertEqual(self.word(4, p2=1).ratio, 0)

    def test_past_the_last_point_the_answer_is_rc_0x11_for_reads_and_writes(self):
        self.use(per_point=True, point_counts=dict(self.COUNTS))
        self.assertEqual(self.tx(8, RD_VF, 0, 7).rc, 0x11)  # [MEASURED lnx 2026-10-02]
        self.assertEqual(self.tx(8, RD_VF, 0, 6).rc, Rc.Pass)
        self.assertEqual(self.tx(4, RD_VF, 0, 8).rc, Rc.Pass)
        self.assertEqual(self.tx(4, RD_VF, 0, 9).rc, 0x11)
        self.enable_per_core()
        self.assertEqual(self.write_vf(8, VfWord(46, 0, False, -5), p2=7).rc, 0x11)
        self.assertEqual(self.port.vf(8, 0, 7), VfWord(0, 0, False, 0))  # nothing stored
        self.assertEqual(self.tx(8, RD_VF, 0, 16).rc, Rc.InvalidParam)  # beyond the 15 of the register map: the old answer

    def test_the_default_config_keeps_the_legacy_1_to_15(self):
        self.use(per_point=True)
        self.assertEqual(self.tx(4, RD_VF, 0, 15).rc, Rc.Pass)
        self.assertIsNone(SimConfig().point_counts)

    def test_measured_config_knows_the_point_counts(self):
        self.port = SimMsrPort(SimConfig.measured(per_point=True))
        self.assertEqual(self.tx(8, RD_VF, 0, 7).rc, 0x11)
        self.assertEqual(self.tx(8, RD_VF, 0, 6).data, 0x2E)

    def test_a_point_offset_write_keeps_the_ratio_and_does_not_touch_the_oc_point(self):
        self.use(per_point=True, point_counts=dict(self.COUNTS), reject_zero_fields=True)
        self.enable_per_core()
        oc_before = self.word(8)
        self.assertEqual(self.write_vf(8, VfWord(46, 0, False, -10), p2=6).rc, Rc.Pass)  # V target 0: accepted even with reject_zero_fields (OCCT, measured)
        self.assertEqual(self.word(8, p2=6), VfWord(46, 0, False, -10))
        self.assertEqual(self.word(8), oc_before)
        self.assertEqual(self.word(9, p2=6), VfWord(46, 0, False, 0))  # per-CPU state (e_cluster_wide is off)

    def test_the_oc_point_still_rejects_zero_fields(self):
        self.use(per_point=True, reject_zero_fields=True)
        self.enable_per_core()
        self.assertEqual(self.write_vf(8, VfWord(48, 0, False, -10)).rc, Rc.InvalidParam)

    def test_a_bad_point_config_is_refused(self):
        for cfg in ({"point_ratios": {("X", 1): 40}}, {"point_ratios": {("E", 0): 40}}, {"point_ratios": {("E", 1): 256}},
                    {"point_counts": {"E": 16}}, {"point_counts": {"Z": 1}}):
            with self.subTest(cfg=cfg), self.assertRaises(ValueError):
                SimConfig(**cfg)


if __name__ == "__main__":
    unittest.main()
