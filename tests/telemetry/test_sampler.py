import json
import tempfile
import time
import unittest
from pathlib import Path

from arltuner.telemetry import Sampler, TelemetryMsr

from ._support import Clock, FakeMsrPort


class SamplerTests(unittest.TestCase):
    def test_no_msr_no_sysfs_everything_unavailable(self):
        with tempfile.TemporaryDirectory() as td:
            s = Sampler(None, hwmon_root=Path(td) / "h", powercap_root=Path(td) / "p")
            snap = s.collect()
        json.dumps(snap)
        self.assertEqual(len(snap["cores"]), 24)
        for c in snap["cores"].values():
            for k in ("mhz", "busy", "vid", "ratio"):
                self.assertEqual(c[k]["status"], "unavailable")
                self.assertIsNone(c[k]["value"])
        self.assertEqual(snap["vcore"]["status"], "unavailable")
        self.assertEqual(snap["power"]["package"]["status"], "unavailable")
        self.assertEqual(snap["dram"]["read"]["status"], "unavailable")

    def test_with_fake_msr(self):
        clk = Clock()
        port = FakeMsrPort(default={0xCE: 0x2500, 0x198: (0x2000 << 32) | (0x30 << 8), 0xE7: 0, 0xE8: 0,
                                    0x1A2: 0x80691400, 0x19C: (1 << 31) | (20 << 16), 0x1B1: (1 << 31) | (25 << 16),
                                    0x606: 0xE0E03, 0x611: 0, 0x6B0: 0})
        with tempfile.TemporaryDirectory() as td:
            s = Sampler(TelemetryMsr(port), hwmon_root=Path(td) / "h", powercap_root=Path(td) / "p", clock=clk)
            s.collect()
            clk.t += 1
            port.default.update({0xE7: 3_700_000_000, 0xE8: 3_700_000_000, 0x611: 16384 * 50})
            snap = s.collect()
        c0 = snap["cores"][0]
        self.assertAlmostEqual(c0["mhz"]["value"], 3700.0)
        self.assertAlmostEqual(c0["busy"]["value"], 100.0)
        self.assertAlmostEqual(c0["vid"]["value"], 1.0)
        self.assertEqual(snap["power"]["package"]["value"], 50.0)  # MSR fallback (no powercap)
        self.assertEqual(snap["temps"]["Package"]["value"], 80)
        self.assertEqual(snap["temps"]["cpu3"]["value"], 85)
        self.assertEqual([m for _, m in port.reads if m == 0x150], [])

    def test_thread_start_stop(self):
        with tempfile.TemporaryDirectory() as td:
            s = Sampler(None, hwmon_root=Path(td), powercap_root=Path(td), interval_s=0.01)
            s.start()
            for _ in range(200):
                if s.snapshot().get("cores"):
                    break
                time.sleep(0.01)
            s.stop()
        self.assertTrue(s.snapshot().get("cores"))


if __name__ == "__main__":
    unittest.main()
