import tempfile
import unittest
from pathlib import Path

from arltuner.telemetry.hwmon import read_coretemp, read_vcore
from arltuner.telemetry.rapl import PowercapPower

from ._support import Clock


def w(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="ascii")


class Hwmon(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)

    def test_coretemp(self):
        w(self.root / "hwmon3/name", "coretemp\n")
        w(self.root / "hwmon3/temp1_label", "Package id 0\n")
        w(self.root / "hwmon3/temp1_input", "45500\n")
        w(self.root / "hwmon3/temp2_label", "Core 0\n")
        w(self.root / "hwmon3/temp2_input", "garbage\n")
        w(self.root / "hwmon3/temp3_label", "Core 1\n")  # input missing
        w(self.root / "hwmon1/name", "nvme\n")
        r = read_coretemp(self.root)
        self.assertEqual(r["Package id 0"].value, 45.5)
        self.assertIsNone(r["Core 0"].value)
        self.assertIsNone(r["Core 1"].value)
        self.assertEqual(read_coretemp(self.root / "nope"), {})

    def test_vcore(self):
        self.assertIsNone(read_vcore(self.root).value)  # empty root
        w(self.root / "hwmon5/name", "nct6799\n")
        w(self.root / "hwmon5/in0_label", "AVCC\n")
        w(self.root / "hwmon5/in0_input", "3300\n")
        self.assertIsNone(read_vcore(self.root).value)  # no Vcore label -> unavailable, not another rail
        w(self.root / "hwmon5/in1_label", "Vcore\n")
        w(self.root / "hwmon5/in1_input", "1296\n")
        r = read_vcore(self.root)
        self.assertAlmostEqual(r.value, 1.296)
        self.assertEqual(r.unit, "V")


class Powercap(unittest.TestCase):
    def test_power_wrap_and_missing(self):
        with tempfile.TemporaryDirectory() as td:
            z = Path(td) / "intel-rapl:0"
            w(z / "name", "package-0\n")
            w(z / "max_energy_range_uj", "1000000\n")
            w(z / "energy_uj", "900000\n")
            clk = Clock()
            p = PowercapPower("intel-rapl:0", Path(td), clk)
            self.assertFalse(p.sample().available)
            clk.t += 2.0
            w(z / "energy_uj", "100000\n")  # wrapped: 200001 uJ in 2 s
            self.assertAlmostEqual(p.sample().value, 0.1000005, places=6)
            self.assertEqual(p.name(), "package-0")
            (z / "energy_uj").unlink()
            clk.t += 1
            r = p.sample()
            self.assertIsNone(r.value)
            self.assertIn("energy_uj", r.reason)
            self.assertIsNone(PowercapPower("intel-rapl:9", Path(td), clk).sample().value)


if __name__ == "__main__":
    unittest.main()
