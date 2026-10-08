import errno
import unittest

from arltuner.msr.port import MsrError
from arltuner.telemetry import cpu, msrio
from arltuner.telemetry.msrio import TelemetryMsr
from arltuner.telemetry.rapl import MsrPackagePower

from ._support import Clock, FakeMsrPort


class TelemetryMsrGuard(unittest.TestCase):
    def test_mailbox_and_unlisted_refused_and_no_write(self):
        port = FakeMsrPort(default={0x150: 1, 0x1AD: 2})
        t = TelemetryMsr(port)
        for bad in (0x150, 0x1AD, 0x620):
            with self.assertRaises(MsrError) as cm:
                t.rdmsr(0, bad)
            self.assertEqual(cm.exception.errno, errno.EPERM)
        self.assertEqual(port.reads, [])
        self.assertFalse(hasattr(t, "wrmsr"))
        self.assertNotIn(0x150, msrio.TELEMETRY_MSRS)


class Freq(unittest.TestCase):
    def setUp(self):
        self.port, self.clk = FakeMsrPort(), Clock()
        self.fs = cpu.FreqSampler(TelemetryMsr(self.port), 3700, self.clk)

    def set(self, c, a, m):
        self.port.values[(c, msrio.MSR_APERF)] = a
        self.port.values[(c, msrio.MSR_MPERF)] = m

    def test_first_sample_unavailable_then_values(self):
        self.set(0, 0, 0)
        mhz, busy = self.fs.sample([0])[0]
        self.assertFalse(mhz.available)
        self.clk.t += 1.0
        self.set(0, 5_000_000_000, 3_700_000_000 // 2)  # busy 50%
        mhz, busy = self.fs.sample([0])[0]
        self.assertAlmostEqual(mhz.value, 3700 * 5_000_000_000 / 1_850_000_000, places=3)
        self.assertAlmostEqual(busy.value, 50.0, places=3)

    def test_idle_and_wrap_and_missing(self):
        self.set(1, 10, 10)
        self.fs.sample([1])
        self.clk.t += 1
        mhz, busy = self.fs.sample([1])[1]
        self.assertIsNone(mhz.value)
        self.assertEqual(busy.value, 0.0)
        self.set(2, (1 << 64) - 10, (1 << 64) - 10)
        self.fs.sample([2])
        self.clk.t += 1
        self.set(2, 90, 90)  # wrapped: delta 100
        mhz, _ = self.fs.sample([2])[2]
        self.assertAlmostEqual(mhz.value, 3700.0)
        mhz, busy = self.fs.sample([5])[5]  # no values for cpu 5 -> EIO
        self.assertFalse(mhz.available)
        self.assertFalse(busy.available)
        self.assertIn("unimplemented", mhz.reason)


class Vid(unittest.TestCase):
    def test_decode_and_missing(self):
        port = FakeMsrPort({(3, 0x198): (0x2AAA << 32) | (0x2F << 8)})
        vid, ratio = cpu.read_vid(TelemetryMsr(port), 3)
        self.assertAlmostEqual(vid.value, 0x2AAA / 8192)
        self.assertEqual(ratio.value, 0x2F)
        vid, ratio = cpu.read_vid(TelemetryMsr(port), 4)
        self.assertIsNone(vid.value)
        self.assertIsNone(ratio.value)


class Temps(unittest.TestCase):
    def test_msr_temps(self):
        port = FakeMsrPort({(0, 0x1A2): 0x80691400, (2, 0x19C): (1 << 31) | (30 << 16), (3, 0x19C): 30 << 16,
                            (0, 0x1B1): (1 << 31) | (45 << 16)})
        m = TelemetryMsr(port)
        tj = cpu.read_tjmax(m)
        self.assertEqual(tj.value, 105)
        self.assertEqual(cpu.read_core_temp_msr(m, 2, tj).value, 75)
        self.assertIn("valid", cpu.read_core_temp_msr(m, 3, tj).reason)  # valid bit clear -> no number
        self.assertEqual(cpu.read_package_temp_msr(m, 0, tj).value, 60)
        nj = cpu.read_tjmax(TelemetryMsr(FakeMsrPort()))
        self.assertFalse(nj.available)
        self.assertFalse(cpu.read_core_temp_msr(m, 2, nj).available)

    def test_tsc(self):
        self.assertEqual(cpu.tsc_mhz(TelemetryMsr(FakeMsrPort({(0, 0xCE): 0x08040838F1812500}))).value, 3700)
        self.assertFalse(cpu.tsc_mhz(TelemetryMsr(FakeMsrPort())).available)

    def test_limit_reasons_raw(self):
        self.assertEqual(cpu.read_limit_reasons_raw(TelemetryMsr(FakeMsrPort({(0, 0x6B0): 0x1234}))).value, 0x1234)
        self.assertFalse(cpu.read_limit_reasons_raw(TelemetryMsr(FakeMsrPort())).available)


class MsrRapl(unittest.TestCase):
    def test_power_and_wrap(self):
        port, clk = FakeMsrPort({(0, 0x606): 0x000E0E03}), Clock()  # ESU = 14
        p = MsrPackagePower(TelemetryMsr(port), 0, clk)
        port.values[(0, 0x611)] = 0xFFFFFF00
        self.assertFalse(p.sample().available)
        clk.t += 2.0
        port.values[(0, 0x611)] = (1 << 14) * 10  # wrapped: 0x100 + 10 J worth of counts
        r = p.sample()
        self.assertAlmostEqual(r.value, ((0x100 + (1 << 14) * 10) / 16384) / 2.0, places=6)
        self.assertFalse(MsrPackagePower(TelemetryMsr(FakeMsrPort()), 0, clk).sample().available)


if __name__ == "__main__":
    unittest.main()
