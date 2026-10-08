"""T-023: the simulator's plain registers (turbo ratio tables, PLATFORM_INFO, FLEX_RATIO). ``[MEASURED lnx 2026-10-01]`` values; SIM model rules."""
from __future__ import annotations

import unittest

from arltuner.msr import MsrError, SimConfig, SimMsrPort

MEASURED = {
    0x1AD: 0x3737373737373939,
    0x1AE: 0x0807060504030201,
    0x650: 0x3030303030303030,
    0x651: 0x100C080604030201,
    0xCE: 0x08040838F1812500,
    0x194: 0x000E0000,
}


class PlainRegisters(unittest.TestCase):
    def test_the_measured_values_are_readable_on_every_cpu_in_both_configurations(self) -> None:
        for config in (SimConfig(), SimConfig.measured()):
            sim = SimMsrPort(config)
            for msr, value in MEASURED.items():
                for cpu in (0, 8, 20, 23):
                    self.assertEqual(sim.rdmsr(cpu, msr), value, (hex(msr), cpu))

    def test_the_tables_are_package_scoped_a_write_on_any_cpu_is_seen_on_all(self) -> None:
        sim = SimMsrPort(SimConfig())
        sim.wrmsr(0, 0x650, 0x3131313131313131)
        self.assertEqual([sim.rdmsr(c, 0x650) for c in (0, 8, 20)], [0x3131313131313131] * 3)
        sim.wrmsr(20, 0x1AD, 0x3A3A383838383838)
        self.assertEqual([sim.rdmsr(c, 0x1AD) for c in (0, 4, 8)], [0x3A3A383838383838] * 3)
        self.assertEqual(sim.rdmsr(0, 0x1AE), MEASURED[0x1AE])  # the others are untouched

    def test_per_cpu_option_keeps_each_cpu_apart(self) -> None:
        sim = SimMsrPort(SimConfig(plain_msrs_per_cpu=True))
        sim.wrmsr(0, 0x650, 0x3131313131313131)
        self.assertEqual(sim.rdmsr(0, 0x650), 0x3131313131313131)
        self.assertEqual(sim.rdmsr(8, 0x650), MEASURED[0x650])
        self.assertEqual(sim.plain_msr(20, 0x650), MEASURED[0x650])

    def test_the_count_registers_and_platform_info_are_read_only(self) -> None:
        sim = SimMsrPort(SimConfig())
        for msr in (0x1AE, 0x651, 0xCE, 0x194):
            with self.assertRaises(MsrError):
                sim.wrmsr(0, msr, 1)
            self.assertEqual(sim.rdmsr(0, msr), MEASURED[msr])

    def test_start_values_can_be_overridden_and_the_registers_switched_off(self) -> None:
        sim = SimMsrPort(SimConfig(plain_msr_values={0x650: 0x2F2F2F2F2F2F2F2F}))
        self.assertEqual(sim.rdmsr(8, 0x650), 0x2F2F2F2F2F2F2F2F)
        off = SimMsrPort(SimConfig(plain_msrs=False))
        with self.assertRaises(MsrError):
            off.rdmsr(0, 0x650)
        with self.assertRaises(ValueError):
            off.plain_msr(0, 0x650)

    def test_a_write_can_be_silently_ignored(self) -> None:
        sim = SimMsrPort(SimConfig())
        sim.ignore_plain_writes(0x650)
        sim.wrmsr(0, 0x650, 0x3131313131313131)  # accepted ...
        self.assertEqual(sim.rdmsr(0, 0x650), MEASURED[0x650])  # ... but not stored
        self.assertEqual(sim.writes, [(0, 0x650, 0x3131313131313131)])
        sim.clear_faults()
        sim.wrmsr(0, 0x650, 0x3131313131313131)
        self.assertEqual(sim.rdmsr(0, 0x650), 0x3131313131313131)

    def test_the_peek_is_not_a_device_read_and_checks_its_arguments(self) -> None:
        sim = SimMsrPort(SimConfig())
        self.assertEqual(sim.plain_msr(0, 0x650), MEASURED[0x650])
        self.assertEqual(sim.reads, [])
        with self.assertRaises(ValueError):
            sim.plain_msr(99, 0x650)
        with self.assertRaises(ValueError):
            sim.plain_msr(0, 0x1A0)

    def test_the_mailbox_and_other_unconfigured_msrs_are_unchanged(self) -> None:
        sim = SimMsrPort(SimConfig())
        with self.assertRaises(MsrError):
            sim.rdmsr(0, 0x1A0)
        sim.set_msr(0, 0x1A0, 5)
        self.assertEqual(sim.rdmsr(0, 0x1A0), 5)


if __name__ == "__main__":
    unittest.main()
